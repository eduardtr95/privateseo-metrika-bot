from __future__ import annotations

import html
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from .analysis import ReportBuilder, ReportData, format_report, format_rich_report, goal_relevance
from .config import Config
from .db import Database
from .telegram import TelegramAPI, TelegramAPIError
from .yandex import YandexAPIError, YandexClient


log = logging.getLogger(__name__)
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


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
        self.reports = ReportBuilder(yandex)
        self.stop_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="bot-update")

    def run_polling(self) -> None:
        offset: int | None = None
        try:
            self.telegram.set_commands()
            self.telegram.set_profile_texts()
        except TelegramAPIError:
            log.exception("Could not update bot profile")
        while not self.stop_event.is_set():
            try:
                updates = self.telegram.get_updates(offset)
                for update in updates:
                    offset = int(update["update_id"]) + 1
                    self.executor.submit(self.handle_update, update)
            except TelegramAPIError:
                log.exception("Telegram polling failed")
                self.stop_event.wait(5)

    def handle_update(self, update: dict) -> None:
        try:
            if "message" in update:
                self._handle_message(update["message"])
            elif "callback_query" in update:
                self._handle_callback(update["callback_query"])
        except (YandexAPIError, TelegramAPIError) as exc:
            chat_id = self._chat_id(update)
            log.warning("Request failed for chat %s: %s", chat_id, exc)
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
        return update.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")

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
            self._welcome(chat_id)
        elif command == "/week":
            self.send_report(chat_id)
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
            self.db.disconnect(chat_id)
            self.telegram.send_message(
                chat_id,
                "Доступ к Метрике удалён из бота. В Яндекс ID его также можно отозвать в разделе доступов.",
            )
        elif command == "/delete_me":
            self.db.delete_user(chat_id)
            self.telegram.send_message(chat_id, "Ваши настройки и OAuth-токены полностью удалены.")
        elif command == "/privacy":
            self.telegram.send_message(
                chat_id,
                "<b>Приватность</b>\n\nБот хранит Telegram chat ID, выбранный счётчик, цели и зашифрованные OAuth-токены. Сырые данные Метрики и отчёты не сохраняются. /disconnect удаляет доступ, /delete_me — все ваши данные.",
            )
        elif command == "/help" or not command:
            self._help(chat_id)
        else:
            self._help(chat_id)

    def _welcome(self, chat_id: int) -> None:
        connection = self.db.get_connection(chat_id)
        if connection:
            self.telegram.send_message(
                chat_id,
                "<b>PrivateSEO Аналитика</b>\n\nМетрика подключена. Я показываю не просто цифры, а существенные изменения: где просел трафик, какие страницы дали рост и что проверить.\n\nЕжедневное или недельное расписание настраивается под вас.",
                [
                    [{"text": "Показать неделю", "callback_data": "week"}],
                    [{"text": "Настроить расписание", "callback_data": "schedule"}],
                    [{"text": "Выбрать счётчик", "callback_data": "counters"}],
                ],
            )
            return
        url = self.yandex.authorization_url(chat_id)
        self.telegram.send_message(
            chat_id,
            "<b>PrivateSEO Аналитика</b>\n\nПодключите Яндекс Метрику — бот по вашему расписанию объяснит:\n• что изменилось;\n• какой источник или страница повлияли;\n• что стоит проверить.\n\nДоступ только на чтение. Токен хранится зашифрованно, отключить его можно в любой момент.",
            [[{"text": "Подключить Метрику", "url": url}]],
        )

    def _help(self, chat_id: int) -> None:
        self.telegram.send_message(
            chat_id,
            "<b>Как пользоваться</b>\n\n/week — отчёт сейчас\n/counters — выбрать сайт\n/goals — выбрать заявки и продажи\n/schedule — дни и время отчётов\n/pause — выключить автодайджест\n/resume — включить обратно\n/disconnect — удалить доступ к Метрике\n/privacy — какие данные хранятся\n\n<b>Другие продукты PrivateSEO</b>\n"
            '🌐 <a href="https://private-seo.ru/">Сайт SEO- и GEO-агентства</a>\n'
            '🧩 <a href="https://chromewebstore.google.com/detail/privateseo-ai-auditor-seo/nblbceehggefmhkioijdbppdboimoicg">PrivateSEO AI Auditor для Chrome</a>\n'
            "🟢 Следить за падениями, SSL, noindex и robots.txt: "
            f'<a href="{html.escape(self.config.monitor_bot_url, quote=True)}">мониторинг сайтов</a>.',
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
            "Еженедельный — последние 7 полных дней с предыдущими 7."
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

    def send_counters(self, chat_id: int) -> None:
        counters = self.yandex.counters(chat_id)
        if not counters:
            self.telegram.send_message(
                chat_id, "В подключённом аккаунте нет доступных счётчиков Метрики."
            )
            return
        buttons = []
        for counter in counters[:40]:
            name = str(counter.get("name") or counter.get("site") or counter["id"])
            buttons.append([{"text": name[:50], "callback_data": f"counter:{counter['id']}"}])
        self.telegram.send_message(chat_id, "Выберите сайт, по которому нужен отчёт:", buttons)

    def send_goals(self, chat_id: int) -> None:
        connection = self.db.get_connection(chat_id)
        if not connection or not connection["counter_id"]:
            self.telegram.send_message(chat_id, "Сначала выберите счётчик: /counters")
            return
        goals = self.yandex.goals(chat_id, int(connection["counter_id"]))
        selected = set(json.loads(connection["goal_ids"] or "[]"))
        if not goals:
            self.telegram.send_message(chat_id, "В этом счётчике пока нет целей.")
            return
        buttons = []
        goals = sorted(
            goals,
            key=lambda goal: (
                -goal_relevance(str(goal.get("name") or "")),
                str(goal.get("name") or "").casefold(),
            ),
        )
        for goal in goals[:40]:
            goal_id = int(goal["id"])
            mark = "✅" if goal_id in selected else "▫️"
            name = str(goal.get("name") or goal_id)
            recommended = "⭐ " if goal_relevance(name) > 0 else ""
            buttons.append(
                [{"text": f"{mark} {recommended}{name}"[:55], "callback_data": f"goal:{goal_id}"}]
            )
        buttons.append(
            [
                {"text": "⭐ Выбрать рекомендуемые", "callback_data": "goals:recommended"},
                {"text": "Снять всё", "callback_data": "goals:clear"},
            ]
        )
        buttons.append([{"text": "Готово — показать отчёт", "callback_data": "week"}])
        self.telegram.send_message(
            chat_id,
            "Выберите бизнес-действия: заявки, звонки, покупки и чат. ⭐ — рекомендуемые цели. Повторное нажатие снимает выбор.",
            buttons,
        )

    def send_report(self, chat_id: int) -> None:
        connection = self.db.get_connection(chat_id)
        if not connection:
            self._welcome(chat_id)
            return
        if not connection["counter_id"]:
            self.send_counters(chat_id)
            return
        self.telegram.send_message(chat_id, "Собираю отчёт — обычно это занимает несколько секунд…")
        data = self.reports.collect(chat_id, connection)
        self._send_formatted_report(chat_id, data, with_buttons=True)
        self.db.event(chat_id, "report_manual", str(connection["counter_id"]))

    def _send_formatted_report(
        self, chat_id: int, data: ReportData, with_buttons: bool = False
    ) -> None:
        buttons = None
        if with_buttons:
            buttons = [
                [{"text": "Настроить цели", "callback_data": "goals"}],
                [{"text": "Расписание отчётов", "callback_data": "schedule"}],
                [{"text": "Другой счётчик", "callback_data": "counters"}],
            ]
        try:
            self.telegram.send_rich_message(chat_id, format_rich_report(data), buttons)
        except TelegramAPIError:
            log.warning("Rich message unavailable for chat %s; using HTML fallback", chat_id)
            self.telegram.send_message(chat_id, format_report(data), buttons)

    def _handle_callback(self, callback: dict) -> None:
        callback_id = str(callback["id"])
        chat_id = int(callback["message"]["chat"]["id"])
        if callback["message"]["chat"].get("type") != "private":
            self.telegram.answer_callback(callback_id, "Настройки доступны только в личном чате")
            return
        username = callback.get("from", {}).get("username")
        self.db.upsert_user(chat_id, username)
        data = str(callback.get("data") or "")
        delayed_answer = (
            data.startswith("goal:") or data.startswith("goals:") or data.startswith("schedule:")
        )
        if not delayed_answer:
            self.telegram.answer_callback(callback_id)

        if data == "week":
            self.send_report(chat_id)
        elif data == "counters":
            self.send_counters(chat_id)
        elif data == "goals":
            self.send_goals(chat_id)
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
        elif data.startswith("counter:"):
            counter_id = int(data.split(":", 1)[1])
            counters = self.yandex.counters(chat_id)
            match = next((item for item in counters if int(item["id"]) == counter_id), None)
            if not match:
                raise YandexAPIError("Счётчик больше не доступен")
            name = str(match.get("name") or match.get("site") or counter_id)
            self.db.select_counter(chat_id, counter_id, name)
            goals = self.yandex.goals(chat_id, counter_id)
            recommended = [
                int(goal["id"]) for goal in goals if goal_relevance(str(goal.get("name") or "")) > 0
            ][:15]
            self.db.set_goals(chat_id, recommended)
            self.db.event(chat_id, "counter_selected", str(counter_id))
            self.telegram.send_message(chat_id, f"Выбран счётчик: <b>{html.escape(name)}</b>")
            if recommended:
                self.telegram.send_message(
                    chat_id,
                    "Я заранее отметил цели, похожие на заявки и обращения. Проверьте список — выбор можно изменить.",
                )
            self.send_goals(chat_id)
        elif data.startswith("goal:"):
            goal_id = int(data.split(":", 1)[1])
            connection = self.db.get_connection(chat_id)
            if not connection:
                self.telegram.answer_callback(callback_id, "Сначала подключите Метрику")
                self._welcome(chat_id)
                return
            selected, added = self.db.toggle_goal(chat_id, goal_id)
            if added is None:
                self.telegram.answer_callback(callback_id, "Можно выбрать не больше 15 целей")
                return
            self._update_goal_message(callback, set(selected))
            self.telegram.answer_callback(callback_id, "Цель добавлена" if added else "Цель убрана")
        elif data in {"goals:recommended", "goals:clear"}:
            buttons = callback["message"].get("reply_markup", {}).get("inline_keyboard", [])
            recommended = []
            if data == "goals:recommended":
                for row in buttons:
                    for button in row:
                        value = str(button.get("callback_data") or "")
                        if value.startswith("goal:") and "⭐" in str(button.get("text") or ""):
                            recommended.append(int(value.split(":", 1)[1]))
            selected = set(recommended[:15])
            self.db.set_goals(chat_id, list(selected))
            self._update_goal_message(callback, selected)
            response = "Рекомендуемые цели выбраны" if selected else "Все цели сняты"
            self.telegram.answer_callback(callback_id, response)

    def _update_goal_message(self, callback: dict, selected: set[int]) -> None:
        message = callback["message"]
        buttons = message.get("reply_markup", {}).get("inline_keyboard", [])
        for row in buttons:
            for button in row:
                value = str(button.get("callback_data") or "")
                if not value.startswith("goal:"):
                    continue
                goal_id = int(value.split(":", 1)[1])
                label = str(button.get("text") or "")
                for prefix in ("✅ ", "▫️ "):
                    if label.startswith(prefix):
                        label = label[len(prefix) :]
                        break
                mark = "✅" if goal_id in selected else "▫️"
                button["text"] = f"{mark} {label}"[:55]
        self.telegram.edit_message_reply_markup(
            int(message["chat"]["id"]), int(message["message_id"]), buttons
        )

    def run_scheduler(self) -> None:
        timezone = ZoneInfo(self.config.report_timezone)
        while not self.stop_event.is_set():
            now = datetime.now(timezone)
            for row in self.db.scheduled_users():
                if self.stop_event.is_set():
                    break
                frequency = str(row["report_frequency"] or "weekly")
                scheduled_hour = int(row["report_hour"])
                if now.hour < scheduled_hour:
                    continue
                if frequency == "daily":
                    report_key = f"D-{now.date().isoformat()}"
                    days = 1
                else:
                    if now.weekday() != int(row["report_weekday"]):
                        continue
                    report_key = f"W-{now.isocalendar().year}-{now.isocalendar().week:02d}"
                    days = 7
                if str(row["last_report_key"] or "") == report_key:
                    continue
                chat_id = int(row["chat_id"])
                try:
                    data = self.reports.collect(chat_id, row, today=now.date(), days=days)
                    self._send_formatted_report(chat_id, data)
                    self.db.mark_report_sent(chat_id, report_key)
                    self.db.event(chat_id, "report_scheduled", report_key)
                except Exception:
                    log.exception("Scheduled report failed for chat %s", chat_id)
                time.sleep(1)
            self.stop_event.wait(60)

    def stop(self) -> None:
        self.stop_event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
