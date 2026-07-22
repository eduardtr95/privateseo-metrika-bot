from __future__ import annotations

import logging
import signal
import threading

from .bot import BotService
from .config import Config
from .crypto import TokenCipher
from .db import Database
from .telegram import TelegramAPI
from .web import serve
from .yandex import YandexClient


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_env()
    db = Database(config.database_path)
    cipher = TokenCipher(config.token_encryption_key)
    telegram = TelegramAPI(config.telegram_bot_token)
    yandex = YandexClient(config, db, cipher)
    service = BotService(config, db, telegram, yandex)

    threads = [
        threading.Thread(target=service.run_polling, name="telegram-polling", daemon=True),
        threading.Thread(target=service.run_scheduler, name="report-scheduler", daemon=True),
        threading.Thread(target=serve, args=(service,), name="oauth-http", daemon=True),
    ]
    for thread in threads:
        thread.start()

    stopping = threading.Event()

    def stop(*_: object) -> None:
        if not stopping.is_set():
            stopping.set()
            service.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    stopping.wait()


if __name__ == "__main__":
    main()
