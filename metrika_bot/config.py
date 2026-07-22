from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    yandex_client_id: str
    yandex_client_secret: str
    yandex_redirect_uri: str
    token_encryption_key: str
    database_path: Path
    http_host: str = "127.0.0.1"
    http_port: int = 8080
    report_timezone: str = "Europe/Moscow"
    report_weekday: int = 0
    report_hour: int = 9
    monitor_bot_url: str = "https://t.me/private_seo_monitor_bot"

    @classmethod
    def from_env(cls) -> "Config":
        required = {
            "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            "YANDEX_CLIENT_ID": os.environ.get("YANDEX_CLIENT_ID", ""),
            "YANDEX_CLIENT_SECRET": os.environ.get("YANDEX_CLIENT_SECRET", ""),
            "YANDEX_REDIRECT_URI": os.environ.get("YANDEX_REDIRECT_URI", ""),
            "TOKEN_ENCRYPTION_KEY": os.environ.get("TOKEN_ENCRYPTION_KEY", ""),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError("Missing environment variables: " + ", ".join(missing))

        weekday = int(os.environ.get("REPORT_WEEKDAY", "0"))
        hour = int(os.environ.get("REPORT_HOUR", "9"))
        if weekday not in range(7):
            raise RuntimeError("REPORT_WEEKDAY must be 0..6")
        if hour not in range(24):
            raise RuntimeError("REPORT_HOUR must be 0..23")

        return cls(
            telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],
            yandex_client_id=required["YANDEX_CLIENT_ID"],
            yandex_client_secret=required["YANDEX_CLIENT_SECRET"],
            yandex_redirect_uri=required["YANDEX_REDIRECT_URI"],
            token_encryption_key=required["TOKEN_ENCRYPTION_KEY"],
            database_path=Path(os.environ.get("DATABASE_PATH", "./metrika-bot.sqlite3")),
            http_host=os.environ.get("HTTP_HOST", "127.0.0.1"),
            http_port=int(os.environ.get("HTTP_PORT", "8080")),
            report_timezone=os.environ.get("REPORT_TIMEZONE", "Europe/Moscow"),
            report_weekday=weekday,
            report_hour=hour,
            monitor_bot_url=os.environ.get(
                "PRIVATESEO_MONITOR_BOT_URL", "https://t.me/private_seo_monitor_bot"
            ),
        )
