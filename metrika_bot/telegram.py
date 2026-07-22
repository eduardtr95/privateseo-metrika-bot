from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
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
            {"command": "pause", "description": "Остановить автодайджест"},
            {"command": "resume", "description": "Возобновить автодайджест"},
            {"command": "help", "description": "Как пользоваться"},
        ]
        self.call("setMyCommands", {"commands": commands})
