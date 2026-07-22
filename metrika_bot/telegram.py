from __future__ import annotations

import json
import logging
import secrets
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


log = logging.getLogger(__name__)


class TelegramAPIError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> Any:
        request = urllib.request.Request(
            self.base_url + method,
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            raise TelegramAPIError("Telegram API unavailable") from None
        if not body.get("ok"):
            raise TelegramAPIError(str(body.get("description") or "Telegram API error"))
        return body.get("result")

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": 50,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload, timeout=60)

    def send_message(
        self,
        chat_id: int,
        text: str,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        self.call("sendMessage", payload)

    def send_rich_message(
        self,
        chat_id: int,
        rich_html: str,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "rich_message": {
                "html": rich_html,
                "skip_entity_detection": True,
            },
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        self.call("sendRichMessage", payload)

    def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        buttons: list[list[dict[str, str]]],
    ) -> None:
        self.call(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": buttons},
            },
        )

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        buttons: list[list[dict[str, str]]],
    ) -> None:
        self.call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": buttons},
            },
        )

    def set_profile_texts(self) -> None:
        self.call("setMyName", {"name": "PrivateSEO Аналитика"})
        self.call(
            "setMyShortDescription",
            {
                "short_description": (
                    "Понятные отчёты Яндекс Метрики: что изменилось, почему и что проверить."
                )
            },
        )
        self.call(
            "setMyDescription",
            {
                "description": (
                    "Подключите Яндекс Метрику — бот покажет причины изменений: "
                    "источники, посадочные страницы и бизнес-цели. Ежедневные или "
                    "еженедельные отчёты в выбранное время. Доступ только на чтение. "
                    "Open source by PrivateSEO: "
                    "github.com/eduardtr95/privateseo-metrika-bot"
                )
            },
        )

    def set_profile_photo(self, path: Path) -> None:
        boundary = "----PrivateSEOBot" + secrets.token_hex(12)
        photo = json.dumps(
            {"type": "static", "photo": "attach://avatar"}, ensure_ascii=False
        ).encode()
        image = path.read_bytes()
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="photo"\r\n',
                b"Content-Type: application/json\r\n\r\n",
                photo,
                b"\r\n",
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="avatar"; filename="bot-avatar.jpg"\r\n',
                b"Content-Type: image/jpeg\r\n\r\n",
                image,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        request = urllib.request.Request(
            self.base_url + "setMyProfilePhoto",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                description = json.loads(exc.read().decode()).get("description")
            except (json.JSONDecodeError, UnicodeDecodeError):
                description = None
            raise TelegramAPIError(description or "Could not set bot profile photo") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            raise TelegramAPIError("Telegram API unavailable") from None
        if not payload.get("ok"):
            raise TelegramAPIError(str(payload.get("description") or "Telegram API error"))

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        try:
            self.call("answerCallbackQuery", payload)
        except TelegramAPIError:
            log.exception("Could not answer callback")

    def set_commands(self) -> None:
        commands = [
            {"command": "week", "description": "Отчёт за последние 7 дней"},
            {"command": "counters", "description": "Выбрать счётчик"},
            {"command": "goals", "description": "Выбрать цели и заявки"},
            {"command": "schedule", "description": "Настроить расписание"},
            {"command": "pause", "description": "Остановить автодайджест"},
            {"command": "resume", "description": "Возобновить автодайджест"},
            {"command": "help", "description": "Как пользоваться"},
        ]
        self.call("setMyCommands", {"commands": commands})
