from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .analysis import (
    ReportBuilder,
    ReportData,
    format_compact_report,
    format_report,
    format_rich_report,
    goal_relevance,
)
from .dashboard import MODES, SOURCE_LABELS, collect_dashboard, dashboard_text, source_selection
from .dashboard import dashboard_details, ensure_history
from .report_cache import ReportCache
from .charts import render_chart
from .config import Config
from .runtime import KeyedQueue, UserLocks
from .db import Database
from .telegram import TelegramAPI, TelegramAPIError
from .yandex import YandexAPIError, YandexClient


log = logging.getLogger(__name__)
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
START_PAYLOAD_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def counter_button_labels(counters: list[dict]) -> list[str]:
    names = [str(item.get("name") or item.get("site") or item["id"]) for item in counters]
    counts: dict[str, int] = {}
    for name in names:
        key = name.casefold()
        counts[key] = counts.get(key, 0) + 1

    labels = []
    for counter, name in zip(counters, names, strict=True):
        if counts[name.casefold()] == 1:
            labels.append(name[:50])
            continue
        site = str(counter.get("site") or "").strip()
        suffix = (
            f"{site} · #{counter['id']}"
            if site and site.casefold() != name.casefold()
            else f"#{counter['id']}"
        )
        room = max(8, 50 - len(suffix) - 3)
        compact_name = name if len(name) <= room else name[: room - 1] + "…"
        labels.append(f"{compact_name} · {suffix}"[:50])
    return labels


class BotService:
    def __init__(
        self,
        config: Config,
        db: Database,
        telegram: TelegramAPI,
        yandex: YandexClient,
    ):
        self.config = config
        self.db = db
        self.telegram = telegram
        self.yandex = yandex
        self.reports = ReportBuilder(yandex, config.report_timezone)
        self._setup_runtime()

    def _setup_runtime(self):
        self.report_cache = ReportCache()
        self.fetch_locks = {view: UserLocks() for view in MODES}
        self.prefetch = KeyedQueue(1, 8, "bot-prepare")
        self.stop_event = threading.Event()
        self.locks = UserLocks()
        self.updates = KeyedQueue(4, 64, "bot-control")
        self.jobs = KeyedQueue(2, 32, "bot-report")
        self.epochs = {}
        self.busy_notices = {}
        self.health_lock = threading.Lock()
        self.heartbeats = {}

    def heartbeat(self, worker: str):
        with self.health_lock:
            self.heartbeats[worker] = time.monotonic()

    def healthy(self) -> bool:
        with self.health_lock:
            return not self.stop_event.is_set() and all(
                name in self.heartbeats and time.monotonic() - self.heartbeats[name] < 180
                for name in ("polling", "scheduler")
            )

    def current_connection(self, chat_id: int, generation: str):
        row = self.db.get_connection(chat_id)
        return row if row and row["generation"] == generation else None

    def _dispatch_update(self, update: dict):
        chat_id = self._chat_id(update)
        if chat_id is None:
            return
        message = update.get("message", {})
        command = str(message.get("text", "")).split(maxsplit=1)[0:1]
        command = command[0].split("@")[0].lower() if command else ""
        destructive = command in {"/delete_me", "/disconnect"} or "my_chat_member" in update
        if destructive:
            with self.locks.for_user(chat_id):
                self.epochs[chat_id] = self.epochs.get(chat_id, 0) + 1
                self.handle_update(update)
            return
        epoch = self.epochs.get(chat_id, 0)

        def handle():
            with self.locks.for_user(chat_id):
                if self.epochs.get(chat_id, 0) == epoch:
                    self.handle_update(update)

        if not self.updates.submit(chat_id, handle):
            callback = update.get("callback_query")
            if callback:
                self.telegram.answer_callback(
                    str(callback["id"]), "Предыдущее действие ещё выполняется"
                )

    def run_polling(self) -> None:
        offset: int | None = None
        try:
            self.telegram.set_commands()
        except TelegramAPIError:
            log.exception("Could not set bot commands")
        self.heartbeat("polling")
        while not self.stop_event.is_set():
            try:
                updates = self.telegram.get_updates(offset)
                self.heartbeat("polling")
                for update in updates:
                    offset = int(update["update_id"]) + 1
                    self._dispatch_update(update)
            except TelegramAPIError:
                log.exception("Telegram polling failed")
                self.stop_event.wait(5)

    def handle_update(self, update: dict) -> None:
        chat_id = self._chat_id(update)
        if chat_id is None:
            return
        with self.locks.for_user(chat_id):
            self._handle_update_locked(update)

    def _handle_update_locked(self, update: dict) -> None:
        try:
            if "message" in update:
                self._handle_message(update["message"])
            elif "callback_query" in update:
                self._handle_callback(update["callback_query"])
            elif "my_chat_member" in update:
                self._handle_my_chat_member(update["my_chat_member"])
        except (YandexAPIError, TelegramAPIError) as exc:
            chat_id = self._chat_id(update)
            log.warning("Request failed for chat %s: %s", chat_id, exc)
            if chat_id and isinstance(exc, YandexAPIError) and exc.reconnect:
                self.db.report_failure(chat_id, "auth", 3600, reauth=True)
            if chat_id:
                self.telegram.send_message(
                    chat_id,
                    "Не получилось получить данные: "
                    + html.escape(str(exc))
                    + "\n\nПопробуйте ещё раз чуть позже.",
                )
        except Exception:
            log.exception("Unhandled update error")
            chat_id = self._chat_id(update)
            if chat_id:
                try:
                    self.telegram.send_message(
                        chat_id, "Что-то пошло не так. Ошибка уже записана — попробуйте позже."
                    )
                except TelegramAPIError:
                    pass

    @staticmethod
    def _chat_id(update: dict) -> int | None:
        if "message" in update:
            return update["message"].get("chat", {}).get("id")
        if "my_chat_member" in update:
            return update["my_chat_member"].get("chat", {}).get("id")
        return update.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")

    def _handle_my_chat_member(self, membership: dict) -> None:
        chat = membership.get("chat", {})
        if chat.get("type") != "private":
            return
        status = membership.get("new_chat_member", {}).get("status")
        if status in {"left", "kicked"}:
            self.db.delete_user(int(chat["id"]))
            if hasattr(self, "report_cache"):
                self.report_cache.drop(int(chat["id"]))

    def _handle_message(self, message: dict) -> None:
        chat_id = int(message["chat"]["id"])
        if message["chat"].get("type") != "private":
            self.telegram.send_message(
                chat_id,
                "Из-за доступа к Метрике бот работает только в личном чате. Откройте его профиль и нажмите Start.",
            )
            return
        username = message.get("from", {}).get("username")
        self.db.upsert_user(chat_id, username)
        text = str(message.get("text") or "").strip()
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

        if command in ("/start", "/connect"):
            if command == "/start":
                parts = text.split(maxsplit=1)
                payload = parts[1] if len(parts) == 2 else "direct"
                if not START_PAYLOAD_RE.fullmatch(payload):
                    payload = "direct"
                self.db.record_first_start(chat_id, payload)
            self._welcome(chat_id, force_connect=command == "/connect")
        elif command in {"/day", "/week", "/month"}:
            self.send_report(chat_id, view=command[1:])
        elif command == "/sources":
            self.send_sources(chat_id)
        elif command == "/counters":
            self.send_counters(chat_id)
        elif command == "/goals":
            self.send_goals(chat_id)
        elif command == "/schedule":
            self.send_schedule(chat_id)
        elif command == "/pause":
            self.db.toggle_reports(chat_id, False)
            self.telegram.send_message(
                chat_id,
                "Автоматические отчёты выключены. Команда /week продолжает работать.",
            )
        elif command == "/resume":
            self.db.toggle_reports(chat_id, True)
            self.send_schedule(chat_id)
        elif command == "/disconnect":
            self.epochs[chat_id] = self.epochs.get(chat_id, 0) + 1
            self.db.disconnect(chat_id)
            self.report_cache.drop(chat_id)
            self.telegram.send_message(
                chat_id,
                "Доступ к Метрике удалён из бота. В Яндекс ID его также можно отозвать в разделе доступов.",
            )
        elif command == "/delete_me":
            self.epochs[chat_id] = self.epochs.get(chat_id, 0) + 1
            self.db.delete_user(chat_id)
            self.report_cache.drop(chat_id)
            self.telegram.send_message(
                chat_id,
                "Данные удалены из рабочей базы, новые отчёты остановлены. Резервные копии удаляются в течение 14 дней. Старые сообщения Telegram можно удалить в чате.",
            )
        elif command == "/privacy":
            self.telegram.send_message(
                chat_id,
                "<b>Приватность</b>\n\nБот хранит Telegram ID и username, источник первого запуска, выбранный счётчик, цели, источники и вид отчёта, расписание, зашифрованные токены и технические события. Параметры кнопок отчётов хранятся до 30 дней, события — до 90 дней. Сырые выгрузки и тексты отчётов не сохраняются на диск. Агрегаты временно переиспользуются в памяти до 5 минут. /disconnect удаляет доступ и ожидающие подключения, /delete_me — данные из рабочей базы. Резервные копии удаляются в течение 14 дней.",
            )
        elif command == "/feedback":
            self._feedback(chat_id)
        elif command == "/help" or not command:
            self._help(chat_id)
        else:
            self._help(chat_id)

    def _welcome(self, chat_id: int, force_connect: bool = False) -> None:
        connection = self.db.get_connection(chat_id)
        if connection and not force_connect and not connection["reauth_required"]:
            self.telegram.send_message(
                chat_id,
                "<b>PrivateSEO Аналитика</b>\n\nМетрика подключена. Я показываю не просто цифры, а существенные изменения: где просел трафик, на каких страницах вырос трафик и что проверить.\n\nЕжедневное или недельное расписание настраивается под вас.",
                [
                    [{"text": "Открыть отчёт", "callback_data": "week"}],
                    [{"text": "Настроить расписание", "callback_data": "schedule"}],
                    [{"text": "Выбрать счётчик", "callback_data": "counters"}],
                ],
            )
            return
        url = self.yandex.authorization_url(chat_id)
        self.telegram.send_message(
            chat_id,
            "<b>PrivateSEO Аналитика</b>\n\nПодключите Яндекс Метрику — бот по вашему расписанию объяснит:\n• что изменилось;\n• какой источник или страница повлияли;\n• что стоит проверить.\n\nДоступ только на чтение. Бот не может менять счётчики, токен хранится зашифрованно и удаляется по команде /disconnect.\n\n⚠️ Пока Яндекс показывает системное предупреждение «Приложение не проверено». Если вас устраивает доступ только на чтение, подключение можно продолжить.",
            [[{"text": "Подключить Метрику", "url": url}]],
        )

    def _help(self, chat_id: int) -> None:
        self.telegram.send_message(
            chat_id,
            '<b>Как пользоваться</b>\n\n/day — вчера к позавчера\n/week — текущая неделя по вчера\n/month — текущий месяц по вчера\n/sources — источники и график\n/connect — подключить или обновить доступ\n/counters — выбрать сайт\n/goals — выбрать заявки и продажи\n/schedule — дни и время отчётов\n/pause — выключить автодайджест\n/resume — включить обратно\n/disconnect — удалить доступ к Метрике\n/privacy — какие данные хранятся\n/delete_me — удалить свои данные\n/feedback — вопросы и предложения\n\n<b>Обратная связь</b>\nНашли ошибку, чего-то не хватает или есть идея? Напишите Эдуарду: <a href="https://t.me/eduardtr95">@eduardtr95</a>.\n\n<b>Другие продукты PrivateSEO</b>\n'
            '🌐 <a href="https://private-seo.ru/?utm_source=telegram&amp;utm_medium=bot&amp;utm_campaign=metrika_bot&amp;utm_content=help">Сайт SEO- и GEO-агентства</a>\n'
            '🧩 <a href="https://chromewebstore.google.com/detail/privateseo-ai-auditor-seo/nblbceehggefmhkioijdbppdboimoicg">PrivateSEO AI Auditor для Chrome</a>\n'
            "🟢 Следить за падениями, SSL, noindex и robots.txt: "
            f'<a href="{html.escape(self.config.monitor_bot_url, quote=True)}">мониторинг сайтов</a>.',
        )

    def _feedback(self, chat_id: int) -> None:
        self.telegram.send_message(
            chat_id,
            '<b>Вопросы и предложения</b>\n\nНашли ошибку, заметили неточность или хотите предложить функцию? Напишите автору бота — Эдуарду: <a href="https://t.me/eduardtr95">@eduardtr95</a>.',
        )

    def send_schedule(self, chat_id: int, message_id: int | None = None) -> None:
        user = self.db.get_user(chat_id)
        if not user:
            return
        enabled = bool(user["report_enabled"])
        frequency = str(user["report_frequency"] or "weekly")
        weekday = int(user["report_weekday"])
        hour = int(user["report_hour"])
        if not enabled:
            current = "Только вручную"
        elif frequency == "daily":
            current = f"Каждый день в {hour:02d}:00 МСК"
        else:
            current = f"Каждую {WEEKDAYS[weekday]} в {hour:02d}:00 МСК"
        text = (
            "<b>Расписание отчётов</b>\n\n"
            f"Сейчас: <b>{current}</b>\n"
            "Ежедневный отчёт сравнивает вчера с позавчера. "
            "Недельный — текущую неделю по вчера с теми же днями прошлой; по понедельникам — две полные недели."
        )
        buttons = [
            [
                {
                    "text": ("✅ " if enabled and frequency == "daily" else "") + "Каждый день",
                    "callback_data": "schedule:frequency:daily",
                },
                {
                    "text": ("✅ " if enabled and frequency == "weekly" else "") + "Раз в неделю",
                    "callback_data": "schedule:frequency:weekly",
                },
            ],
            [
                {
                    "text": ("✅ " if not enabled else "") + "Только вручную",
                    "callback_data": "schedule:manual",
                }
            ],
        ]
        if frequency == "weekly":
            buttons.append(
                [
                    {
                        "text": ("✅" if day == weekday else "") + WEEKDAYS[day],
                        "callback_data": f"schedule:weekday:{day}",
                    }
                    for day in range(7)
                ]
            )
        buttons.append(
            [
                {"text": "◀ −1 час", "callback_data": "schedule:hour:-1"},
                {"text": f"{hour:02d}:00 МСК", "callback_data": "schedule:noop"},
                {"text": "+1 час ▶", "callback_data": "schedule:hour:1"},
            ]
        )
        buttons.append([{"text": "Прислать отчёт сейчас", "callback_data": "week"}])
        if message_id is None:
            self.telegram.send_message(chat_id, text, buttons)
        else:
            self.telegram.edit_message_text(chat_id, message_id, text, buttons)

    def send_counters(self, chat_id: int, page: int = 0) -> None:
        connection = self.db.get_connection(chat_id)
        if not connection or connection["reauth_required"]:
            self._welcome(chat_id, force_connect=True)
            return
        counters = self.yandex.counters(chat_id)
        if not counters:
            self.telegram.send_message(
                chat_id, "В этом аккаунте нет счётчиков. Подключить другой: /connect"
            )
            return
        page = max(0, min(page, (len(counters) - 1) // 20))
        gen = connection["generation"]
        visible = counters[page * 20 : (page + 1) * 20]
        buttons = [
            [{"text": label, "callback_data": f"c:{gen}:{item['id']}"}]
            for item, label in zip(visible, counter_button_labels(visible), strict=True)
        ]
        buttons.extend(self._pager(page, len(counters), f"cp:{gen}"))
        self.telegram.send_message(
            chat_id,
            f"Выберите сайт · страница {page + 1} из {(len(counters) - 1) // 20 + 1}:",
            buttons,
        )

    @staticmethod
    def _pager(page: int, total: int, prefix: str):
        buttons = []
        if page > 0:
            buttons.append({"text": "← Назад", "callback_data": f"{prefix}:{page - 1}"})
        if (page + 1) * 20 < total:
            buttons.append({"text": "Далее →", "callback_data": f"{prefix}:{page + 1}"})
        return [buttons] if buttons else []

    def send_goals(self, chat_id: int, page: int = 0, message_id: int | None = None) -> None:
        connection = self.db.get_connection(chat_id)
        if not connection or not connection["counter_id"]:
            self.telegram.send_message(chat_id, "Сначала выберите счётчик: /counters")
            return
        goals = self.yandex.goals(chat_id, int(connection["counter_id"]))
        selected = set(json.loads(connection["goal_ids"] or "[]"))
        goals = sorted(
            goals,
            key=lambda g: (
                -goal_relevance(str(g.get("name") or "")),
                str(g.get("name") or "").casefold(),
                int(g["id"]),
            ),
        )
        page = max(0, min(page, max(0, (len(goals) - 1) // 20)))
        gen = connection["generation"]
        buttons = []
        for goal in goals[page * 20 : (page + 1) * 20]:
            goal_id = int(goal["id"])
            mark = "✅" if goal_id in selected else "▫️"
            name = str(goal.get("name") or goal_id)
            star = "⭐ " if goal_relevance(name) > 0 else ""
            buttons.append(
                [
                    {
                        "text": f"{mark} {star}{name}"[:55],
                        "callback_data": f"g:{gen}:{page}:{goal_id}",
                    }
                ]
            )
        buttons.extend(self._pager(page, len(goals), f"gp:{gen}"))
        if goals:
            buttons.append(
                [
                    {"text": "⭐ Рекомендуемые", "callback_data": f"ga:{gen}:{page}:rec"},
                    {"text": "Снять всё", "callback_data": f"ga:{gen}:{page}:clear"},
                ]
            )
        buttons.append([{"text": "Готово · К настройкам", "callback_data": f"cfg:{gen}:home"}])
        text = (
            f"<b>Цели · {html.escape(str(connection['counter_name']))}</b>\n\n"
            f"Выбрано {len(selected)} из 15. Все отмеченные цели входят в итог без дублей. "
            "⭐ — только рекомендация: оставьте действия, важные для вашего сайта.\n"
            f"Страница {page + 1} из {max(1, (len(goals) - 1) // 20 + 1)}.\n\n"
            "✓ Выбор сохраняется сразу"
        )
        if not goals:
            text += "\nВ счётчике пока нет целей; отчёт по трафику доступен."
        if message_id is None:
            self.telegram.send_message(chat_id, text, buttons)
        else:
            self.telegram.edit_message_text(chat_id, message_id, text, buttons)

    def send_report(
        self,
        chat_id: int,
        detailed: bool = False,
        *,
        context_id: str | None = None,
        days: int = 7,
        today: date | None = None,
        scheduled_key: str | None = None,
        view: str | None = None,
        edit_message_id: int | None = None,
        edit_has_photo: bool = False,
        force_chart: bool = False,
    ) -> bool:
        with self.locks.for_user(chat_id):
            row = self.db.get_connection(chat_id)
            if not row or row["reauth_required"]:
                if not scheduled_key:
                    self._welcome(chat_id, force_connect=True)
                return False
            connection = dict(row)
            if not connection["counter_id"]:
                self.send_counters(chat_id)
                return False
            today = today or datetime.now(ZoneInfo(self.config.report_timezone)).date()
            requested_view = view
            from_context = context_id is not None
            view = view or (
                "day"
                if scheduled_key and days == 1
                else "week"
                if scheduled_key
                else connection.get("report_view", "week")
            )
            if view not in MODES:
                return False
            if context_id:
                context = self.db.report_context(chat_id, context_id)
                if not context or context["generation"] != connection["generation"]:
                    self.telegram.send_message(
                        chat_id,
                        "Этот отчёт относится к прежнему подключению или устарел. Новый отчёт: /week",
                    )
                    return False
                params = json.loads(context["payload"])
                connection.update(params["connection"])
                today, days = date.fromisoformat(params["today"]), int(params["days"])
                view = requested_view or params.get("view")
                if requested_view and requested_view != params.get("view"):
                    context_id = None
            if force_chart:
                connection["chart_enabled"] = 1
            if requested_view and not scheduled_key:
                self.db.set_display(chat_id, connection["generation"], view=requested_view)
            if not scheduled_key and not from_context:
                try:
                    self.telegram.send_chat_action(chat_id)
                except TelegramAPIError:
                    pass

            def work():
                self._run_report(
                    chat_id,
                    connection,
                    today,
                    days,
                    detailed,
                    scheduled_key,
                    context_id,
                    view,
                    edit_message_id,
                    edit_has_photo,
                )

            accepted = self.jobs.submit(chat_id, work)
            if not accepted and not scheduled_key:
                now = time.monotonic()
                if now - self.busy_notices.get(chat_id, 0) > 10:
                    self.busy_notices[chat_id] = now
                    self.telegram.send_message(
                        chat_id,
                        "Отчёт уже собирается или очередь занята. Подождите немного — повторные нажатия не нужны.",
                    )
            return accepted

    def _calendar_data(self, chat_id, connection, today, view, detailed=False):
        key = self.report_cache.key(chat_id, connection, today, view)
        # Foreground and preparation of the same period share one fetch.
        with self.fetch_locks[view].for_user(hash(key)):
            data = self.report_cache.get(key)
            if data is None:
                data = collect_dashboard(
                    self.reports,
                    chat_id,
                    connection,
                    today,
                    view,
                    chart=False,
                    include_pages=False,
                    fast=True,
                )
            if detailed and not data.pages_loaded:
                self.reports.add_pages(chat_id, int(connection["counter_id"]), data)
            data.dashboard.chart_enabled = bool(not detailed and connection.get("chart_enabled", 1))
            if data.dashboard.chart_enabled:
                ensure_history(self.reports, chat_id, connection, data)
            with self.locks.for_user(chat_id):
                row = self.current_connection(chat_id, connection["generation"])
                if self.stop_event.is_set() or not row or row["reauth_required"]:
                    raise YandexAPIError("Подключение изменилось. Откройте новый отчёт.")
                self.report_cache.put(key, data)
            return data

    def _prepare_views(self, chat_id, connection, today, shown_view):
        for view in ("week", "month", "day"):
            if view == shown_view:
                continue
            with self.locks.for_user(chat_id):
                row = self.current_connection(chat_id, connection["generation"])
                if self.stop_event.is_set() or not row or row["reauth_required"]:
                    return
            try:
                with self.yandex.report_scope(chat_id, connection["generation"]):
                    self._calendar_data(chat_id, connection, today, view)
            except Exception as exc:
                # Preparation never sends messages or holds the foreground queue.
                log.info("Period preparation stopped (%s)", type(exc).__name__)
                return

    def _run_report(
        self,
        chat_id,
        connection,
        today,
        days,
        detailed,
        scheduled_key,
        context_id,
        view=None,
        edit_message_id=None,
        edit_has_photo=False,
    ):
        generation = connection["generation"]
        with self.locks.for_user(chat_id):
            if self.stop_event.is_set() or not self.current_connection(chat_id, generation):
                return
        try:
            with self.yandex.report_scope(chat_id, generation):
                if view:
                    data = self._calendar_data(chat_id, connection, today, view, detailed)
                else:
                    data = self.reports.collect(chat_id, connection, today=today, days=days)
            with self.locks.for_user(chat_id):
                if self.stop_event.is_set() or not self.current_connection(chat_id, generation):
                    return
                if scheduled_key:
                    user = self.db.get_user(chat_id)
                    if (
                        not user
                        or not user["report_enabled"]
                        or user["last_report_key"] == scheduled_key
                    ):
                        return
                    now = datetime.now(ZoneInfo(self.config.report_timezone))
                    if self._due_key(user, now) != (scheduled_key, days):
                        return
                if not context_id:
                    context_id = self.db.save_report_context(
                        chat_id,
                        generation,
                        {
                            "connection": {
                                key: connection[key]
                                for key in (
                                    "counter_id",
                                    "counter_name",
                                    "goal_ids",
                                    "visible_sources",
                                    "chart_enabled",
                                    "report_view",
                                )
                                if key in connection
                            },
                            "today": today.isoformat(),
                            "days": days,
                            "view": view,
                        },
                    )
                self._send_formatted_report(
                    chat_id,
                    data,
                    with_buttons=True,
                    detailed=detailed,
                    context_id=context_id,
                    edit_message_id=edit_message_id,
                    edit_has_photo=edit_has_photo,
                )
                if scheduled_key:
                    self.db.mark_report_sent(chat_id, scheduled_key)
                else:
                    self.db.clear_report_failure(chat_id)
                self.db.event(
                    chat_id,
                    "report_scheduled" if scheduled_key else "report_manual",
                    scheduled_key or str(connection["counter_id"]),
                )
                if view and not detailed:
                    self.prefetch.submit(
                        chat_id, lambda: self._prepare_views(chat_id, connection, today, view)
                    )
        except Exception as exc:
            log.warning("Report failed (%s)", type(exc).__name__)
            with self.locks.for_user(chat_id):
                if self.stop_event.is_set() or not self.current_connection(chat_id, generation):
                    return
                auth = isinstance(exc, YandexAPIError) and exc.reconnect
                notice = "auth" if auth else "temporary-" + today.isoformat()
                retry = max(300, getattr(exc, "retry_after", 0))
                notify = self.db.report_failure(chat_id, notice, retry, reauth=auth)
                if not scheduled_key or notify:
                    text = (
                        "Доступ к Метрике нужно обновить: /connect"
                        if auth
                        else "Отчёт пока не получился. Автоматическую отправку повторю позже; вручную — /week."
                    )
                    if not scheduled_key:
                        text = (
                            str(exc)
                            if isinstance(exc, YandexAPIError)
                            else "Отчёт пока не получился. Попробуйте через несколько минут: /week"
                        )
                    try:
                        self.telegram.send_message(chat_id, text)
                    except TelegramAPIError:
                        pass

    def _send_formatted_report(
        self,
        chat_id: int,
        data: ReportData,
        with_buttons: bool = False,
        detailed: bool = False,
        context_id: str | None = None,
        edit_message_id: int | None = None,
        edit_has_photo: bool = False,
    ) -> None:
        if data.dashboard and not detailed:
            self._send_dashboard(chat_id, data, context_id, edit_message_id, edit_has_photo)
            return
        row = []
        if context_id:
            row.append(
                {
                    "text": "Коротко" if detailed else "Подробнее",
                    "callback_data": f"r:{context_id}:{'short' if detailed else 'full'}",
                }
            )
        if with_buttons:
            row.append({"text": "Настройки", "callback_data": "settings"})
        buttons = [row] if row else []
        if not detailed:
            self.telegram.send_message(chat_id, format_compact_report(data), buttons)
            return
        if data.dashboard:
            self.telegram.send_message(chat_id, dashboard_details(data), buttons)
            return
        rich_text = format_rich_report(data)
        try:
            if len(rich_text.encode("utf-8")) > 32768:
                raise TelegramAPIError("Rich text too large")
            self.telegram.send_rich_message(chat_id, rich_text, buttons)
        except TelegramAPIError:
            self.telegram.send_message(chat_id, format_report(data), buttons)

    def _send_dashboard(self, chat_id, data, context_id, message_id=None, has_photo=False):
        from html.parser import HTMLParser

        class Visible(HTMLParser):
            def __init__(self, text):
                super().__init__(convert_charrefs=True)
                self.text = ""
                self.feed(text)

            def handle_data(self, value):
                self.text += value

        buttons = [
            [
                {
                    "text": ("✓ " if data.dashboard.mode == mode else "") + label,
                    "callback_data": f"v:{context_id}:{mode}",
                }
                for mode, label in MODES.items()
            ],
            [
                {"text": "Подробнее", "callback_data": f"r:{context_id}:full"},
                {"text": "Настроить отчёт", "callback_data": "sources"},
            ],
        ]
        text = dashboard_text(data)
        if not data.dashboard.chart_enabled:
            buttons.insert(1, [{"text": "График", "callback_data": f"chart:{context_id}"}])
            if message_id and not has_photo:
                self.telegram.edit_message_text(chat_id, message_id, text, buttons)
            else:
                self.telegram.send_message(chat_id, text, buttons)
            return
        for limit in (3, 2, 1, 0):
            text = dashboard_text(data, goal_limit=limit)
            if len(Visible(text).text.encode("utf-16-le")) // 2 <= 1024:
                break
        caption_fits = len(Visible(text).text.encode("utf-16-le")) // 2 <= 1024
        try:
            png = render_chart(data) if caption_fits else None
        except Exception:
            log.warning("Chart rendering failed; sending text")
            data.dashboard.chart_warning = "График временно недоступен; цифры отчёта получены."
            text = dashboard_text(data, goal_limit=0)
            png = None
        if png:
            try:
                self.telegram.send_chart(chat_id, png, text, buttons, message_id)
                return
            except TelegramAPIError:
                log.warning("Chart delivery failed; sending text")
        if message_id and not has_photo:
            self.telegram.edit_message_text(chat_id, message_id, text, buttons)
        else:
            self.telegram.send_message(chat_id, dashboard_text(data), buttons)

    @staticmethod
    def _source_summary(connection):
        selected = source_selection(connection)
        if selected is None or set(selected) == set(SOURCE_LABELS):
            return "Все источники"
        if not selected:
            return "Источники скрыты"
        return ", ".join(label for key, label in SOURCE_LABELS.items() if key in selected)

    def _settings_message(self, chat_id, text, buttons, message_id):
        if message_id is None:
            self.telegram.send_message(chat_id, text, buttons)
        else:
            self.telegram.edit_message_text(chat_id, message_id, text, buttons)

    def send_sources(self, chat_id, message_id=None):
        """The report settings overview; /sources and older report buttons land here."""
        connection = self.db.get_connection(chat_id)
        if not connection or not connection["counter_id"]:
            self.send_counters(chat_id)
            return
        gen = connection["generation"]
        count = len(json.loads(connection["goal_ids"] or "[]"))
        goals = f"Выбрано {count}" if count else "Не выбраны"
        chart = "Сразу в отчёте" if connection["chart_enabled"] else "По кнопке «График»"
        text = (
            f"<b>Настройка отчёта</b>\n{html.escape(str(connection['counter_name']))}\n\n"
            f"<b>Источники:</b> {html.escape(self._source_summary(connection))}\n"
            f"<b>Цели:</b> {goals}\n"
            f"<b>График:</b> {chart}\n\n"
            "✓ Настройки сохранены"
        )
        buttons = [
            [{"text": "Источники трафика →", "callback_data": f"cfg:{gen}:sources"}],
            [{"text": "Цели →", "callback_data": f"cfg:{gen}:goals"}],
            [{"text": "График →", "callback_data": f"cfg:{gen}:chart"}],
            [{"text": "Показать отчёт", "callback_data": f"ready:{gen}"}],
        ]
        self._settings_message(chat_id, text, buttons, message_id)

    def send_source_picker(self, chat_id, message_id=None):
        connection = self.db.get_connection(chat_id)
        if not connection or not connection["counter_id"]:
            self.send_counters(chat_id)
            return
        selected = source_selection(connection)
        gen = connection["generation"]
        cells = [
            {
                "text": ("✅ " if selected is None or key in selected else "☐ ") + label,
                "callback_data": f"src:{gen}:{key}",
            }
            for key, label in SOURCE_LABELS.items()
        ]
        buttons = [cells[i : i + 2] for i in range(0, len(cells), 2)]
        buttons += [
            [
                {"text": "Выбрать все", "callback_data": f"src:{gen}:all"},
                {"text": "Снять всё", "callback_data": f"src:{gen}:none"},
            ],
            [{"text": "Готово · К настройкам", "callback_data": f"cfg:{gen}:home"}],
        ]
        text = (
            "<b>Источники трафика</b>\nОтметьте источники для списка и графика.\n\n"
            f"<b>Выбрано:</b> {html.escape(self._source_summary(connection))}\n\n"
            "✓ Выбор сохраняется сразу\n"
            "<i>Общие итоги и цели относятся ко всему сайту.</i>"
        )
        self._settings_message(chat_id, text, buttons, message_id)

    def send_chart_settings(self, chat_id, message_id=None):
        connection = self.db.get_connection(chat_id)
        if not connection or not connection["counter_id"]:
            self.send_counters(chat_id)
            return
        gen = connection["generation"]
        automatic = bool(connection["chart_enabled"])
        text = (
            "<b>График в отчёте</b>\n\n"
            "<b>Сразу</b> — картинка вместе с цифрами.\n"
            "<b>По кнопке</b> — сначала цифры; график открывается по нажатию.\n\n"
            "✓ Выбор сохраняется сразу"
        )
        buttons = [
            [
                {
                    "text": ("✅ " if automatic else "☐ ") + "График сразу",
                    "callback_data": f"src:{gen}:chart_auto",
                }
            ],
            [
                {
                    "text": ("✅ " if not automatic else "☐ ") + "График по кнопке",
                    "callback_data": f"src:{gen}:chart_button",
                }
            ],
            [{"text": "Готово · К настройкам", "callback_data": f"cfg:{gen}:home"}],
        ]
        self._settings_message(chat_id, text, buttons, message_id)

    def send_settings(self, chat_id: int) -> None:
        self.telegram.send_message(
            chat_id,
            "<b>Настройки отчётов</b>\nВыберите, что изменить:",
            [
                [{"text": "Настроить отчёт", "callback_data": "sources"}],
                [
                    {"text": "Цели", "callback_data": "goals"},
                    {"text": "Расписание", "callback_data": "schedule"},
                ],
                [{"text": "Выбрать сайт", "callback_data": "counters"}],
            ],
        )

    def _handle_callback(self, callback: dict) -> None:
        callback_id = str(callback["id"])
        chat_id = int(callback["message"]["chat"]["id"])
        if callback["message"]["chat"].get("type") != "private":
            self.telegram.answer_callback(callback_id, "Настройки доступны только в личном чате")
            return
        self.db.upsert_user(chat_id, callback.get("from", {}).get("username"))
        data = str(callback.get("data") or "")
        if data.startswith(("goal:", "goals:", "counter:")) or data == "week:full":
            self.telegram.answer_callback(
                callback_id, "Кнопки устарели. Откройте новые настройки или /week"
            )
            self.telegram.send_message(
                chat_id,
                "Это кнопки прежней версии. Откройте /goals, /counters или новый отчёт /week.",
            )
            return
        loading = (
            "Загружаю график…"
            if data.startswith("chart:")
            else "Загружаю отчёт…"
            if data.startswith(("v:", "r:", "ready:")) or data == "week"
            else None
        )
        self.telegram.answer_callback(callback_id, loading)
        if data == "week":
            self.send_report(chat_id)
        elif data == "sources":
            self.send_sources(chat_id)
        elif data.startswith("v:"):
            parts = data.split(":")
            if len(parts) == 3 and parts[2] in MODES:
                self.send_report(
                    chat_id,
                    context_id=parts[1],
                    view=parts[2],
                    edit_message_id=int(callback["message"]["message_id"]),
                    edit_has_photo=bool(callback["message"].get("photo")),
                    force_chart=bool(callback["message"].get("photo")),
                )
        elif data.startswith("chart:"):
            parts = data.split(":")
            if len(parts) == 2:
                self.send_report(chat_id, context_id=parts[1], force_chart=True)
        elif data.startswith("cfg:"):
            parts = data.split(":")
            connection = self.db.get_connection(chat_id)
            if len(parts) != 3 or not connection or connection["generation"] != parts[1]:
                self.telegram.send_message(chat_id, "Эти настройки устарели. Откройте /sources.")
                return
            message_id = int(callback["message"]["message_id"])
            if parts[2] == "home":
                self.send_sources(chat_id, message_id)
            elif parts[2] == "sources":
                self.send_source_picker(chat_id, message_id)
            elif parts[2] == "chart":
                self.send_chart_settings(chat_id, message_id)
            elif parts[2] == "goals":
                self.send_goals(chat_id, message_id=message_id)
        elif data.startswith("src:"):
            parts = data.split(":")
            connection = self.db.get_connection(chat_id)
            if len(parts) != 3 or not connection or connection["generation"] != parts[1]:
                self.telegram.send_message(chat_id, "Эти настройки устарели. Откройте /sources.")
                return
            key = parts[2]
            if key == "chart":
                self.db.set_display(chat_id, parts[1], chart=not connection["chart_enabled"])
            elif key in {"chart_auto", "chart_button"}:
                self.db.set_display(chat_id, parts[1], chart=key == "chart_auto")
            elif key == "all":
                self.db.set_display(chat_id, parts[1], all_sources=True)
            elif key == "none":
                self.db.set_display(chat_id, parts[1], sources=[])
            elif key in SOURCE_LABELS:
                selected = source_selection(connection)
                selected = set(SOURCE_LABELS if selected is None else selected)
                selected.symmetric_difference_update({key})
                self.db.set_display(chat_id, parts[1], sources=list(selected))
            message_id = int(callback["message"]["message_id"])
            if key in {"chart", "chart_auto", "chart_button"}:
                self.send_chart_settings(chat_id, message_id)
            else:
                self.send_source_picker(chat_id, message_id)
        elif data.startswith("r:"):
            parts = data.split(":")
            if len(parts) == 3 and parts[2] in {"short", "full"}:
                self.send_report(chat_id, detailed=parts[2] == "full", context_id=parts[1])
        elif data == "counters":
            self.send_counters(chat_id)
        elif data == "goals":
            self.send_goals(chat_id)
        elif data == "settings":
            self.send_settings(chat_id)
        elif data == "schedule":
            self.send_schedule(chat_id)
        elif data.startswith("schedule:"):
            parts = data.split(":")
            if data == "schedule:noop":
                self.telegram.answer_callback(callback_id)
                return
            if parts[1] == "frequency":
                self.db.set_report_schedule(chat_id, frequency=parts[2], enabled=True)
            elif parts[1] == "manual":
                self.db.set_report_schedule(chat_id, enabled=False)
            elif parts[1] == "weekday":
                self.db.set_report_schedule(chat_id, weekday=int(parts[2]))
            elif parts[1] == "hour":
                user = self.db.get_user(chat_id)
                hour = (int(user["report_hour"]) + int(parts[2])) % 24
                self.db.set_report_schedule(chat_id, hour=hour)
            self.send_schedule(chat_id, int(callback["message"]["message_id"]))
            self.telegram.answer_callback(callback_id, "Расписание обновлено")
        elif data.split(":", 1)[0] in {"c", "cp", "g", "gp", "ga", "ready"}:
            parts = data.split(":")
            connection = self.db.get_connection(chat_id)
            if len(parts) < 2 or not connection or parts[1] != connection["generation"]:
                self.telegram.send_message(
                    chat_id,
                    "Сайт или подключение изменились. Откройте актуальные настройки: /counters или /goals.",
                )
                return
            action = parts[0]
            if action == "ready":
                self.send_sources(chat_id, int(callback["message"]["message_id"]))
                self.send_report(chat_id)
            elif action == "cp":
                self.send_counters(chat_id, int(parts[2]))
            elif action == "c":
                counter_id = int(parts[2])
                counters = self.yandex.counters(chat_id)
                match = next((c for c in counters if int(c["id"]) == counter_id), None)
                if not match:
                    raise YandexAPIError("Счётчик больше не доступен. Выберите другой: /counters")
                self.db.select_counter(
                    chat_id, counter_id, str(match.get("name") or match.get("site") or counter_id)
                )
                self.db.clear_report_failure(chat_id)
                goals = self.yandex.goals(chat_id, counter_id)
                recommended = [
                    int(g["id"])
                    for g in sorted(goals, key=lambda g: -goal_relevance(str(g.get("name") or "")))
                    if goal_relevance(str(g.get("name") or "")) > 0
                ][:15]
                self.db.set_goals(chat_id, recommended)
                self.db.event(chat_id, "counter_selected", str(counter_id))
                self.send_goals(chat_id)
            else:
                page = int(parts[2])
                if action != "gp":
                    goals = self.yandex.goals(chat_id, int(connection["counter_id"]))
                    valid = {int(g["id"]) for g in goals}
                    if action == "g":
                        goal_id = int(parts[3])
                        if goal_id not in valid:
                            self.telegram.send_message(
                                chat_id, "Эта цель больше не доступна. Обновите список: /goals"
                            )
                            return
                        _, added = self.db.toggle_goal(chat_id, goal_id)
                        if added is None:
                            self.telegram.send_message(
                                chat_id, "Можно выбрать не больше 15 целей. Снимите лишнюю цель."
                            )
                            return
                    elif action == "ga":
                        selected = (
                            [
                                int(g["id"])
                                for g in sorted(
                                    goals, key=lambda g: -goal_relevance(str(g.get("name") or ""))
                                )
                                if goal_relevance(str(g.get("name") or "")) > 0
                            ][:15]
                            if parts[3] == "rec"
                            else []
                        )
                        self.db.set_goals(chat_id, selected)
                self.send_goals(chat_id, page, int(callback["message"]["message_id"]))

    @staticmethod
    def _due_key(user, now):
        if not user["report_enabled"] or now.hour < int(user["report_hour"]):
            return None
        if user["report_frequency"] == "daily":
            return f"D-{now.date().isoformat()}", 1
        if now.weekday() != int(user["report_weekday"]):
            return None
        return f"W-{now.isocalendar().year}-{now.isocalendar().week:02d}", 7

    def scheduler_tick(self):
        now = datetime.now(ZoneInfo(self.config.report_timezone))
        self.db.cleanup()
        self.report_cache.prune()
        for row in self.db.scheduled_users():
            if self.stop_event.is_set():
                break
            due = self._due_key(row, now)
            if due and row["last_report_key"] != due[0]:
                self.send_report(
                    int(row["chat_id"]), days=due[1], today=now.date(), scheduled_key=due[0]
                )

    def run_scheduler(self) -> None:
        self.heartbeat("scheduler")
        while not self.stop_event.is_set():
            try:
                self.scheduler_tick()
                self.heartbeat("scheduler")
            except Exception:
                log.exception("Scheduler cycle failed; will retry")
            self.stop_event.wait(60)

    def complete_oauth(self, stored, tokens) -> bool:
        chat_id = int(stored["chat_id"])
        with self.locks.for_user(chat_id):
            user = self.db.get_user(chat_id)
            if not user or user["epoch"] != stored["user_epoch"]:
                return False
            self.report_cache.drop(chat_id)
            self.yandex.save_tokens(chat_id, tokens)
            self.db.clear_report_failure(chat_id)
            self.db.event(chat_id, "oauth_connected")
            self.telegram.send_message(chat_id, "✅ Метрика подключена. Теперь выберите сайт:")
            self.send_counters(chat_id)
            return True

    def stop(self) -> None:
        self.stop_event.set()
        self.updates.stop()
        self.jobs.stop()
        self.prefetch.stop()
