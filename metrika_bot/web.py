from __future__ import annotations

import html
import logging
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .bot import BotService
from .yandex import YandexAPIError


log = logging.getLogger(__name__)


def make_handler(service: BotService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health":
                healthy = service.healthy()
                self._reply(
                    HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE,
                    "ok" if healthy else "degraded",
                    "text/plain; charset=utf-8",
                )
                return
            if parsed.path != "/oauth/callback":
                self._reply(HTTPStatus.NOT_FOUND, "not found", "text/plain; charset=utf-8")
                return
            query = urllib.parse.parse_qs(parsed.query)
            state = (query.get("state") or [""])[0]
            code = (query.get("code") or [""])[0]
            error = (query.get("error_description") or query.get("error") or [""])[0]
            if error:
                service.db.consume_oauth_state(state)
                self._page(HTTPStatus.BAD_REQUEST, "Доступ не предоставлен", error)
                return
            stored = service.db.consume_oauth_state(state)
            if not stored or not code:
                self._page(
                    HTTPStatus.BAD_REQUEST,
                    "Ссылка устарела",
                    "Вернитесь в Telegram и нажмите «Подключить Метрику» ещё раз.",
                )
                return
            try:
                tokens = service.yandex.exchange_code(code, str(stored["code_verifier"]))
                if not service.complete_oauth(stored, tokens):
                    self._page(
                        HTTPStatus.BAD_REQUEST,
                        "Подключение отменено",
                        "Запрос уже отменён или заменён. Вернитесь в Telegram и подключитесь заново.",
                    )
                    return

            except Exception as exc:
                log.exception("OAuth callback failed")
                detail = (
                    str(exc)
                    if isinstance(exc, YandexAPIError)
                    else "Попробуйте подключить Метрику ещё раз."
                )
                self._page(HTTPStatus.BAD_GATEWAY, "Не удалось подключить", detail)
                return
            self._page(
                HTTPStatus.OK,
                "Метрика подключена",
                "Можно закрыть эту страницу и вернуться в Telegram — бот уже прислал выбор сайтов.",
            )

        def _page(self, status: HTTPStatus, title: str, body: str) -> None:
            document = f"""<!doctype html><html lang=\"ru\"><meta charset=\"utf-8\">
            <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
            <title>{html.escape(title)}</title><style>
            body{{font:17px/1.5 system-ui,sans-serif;background:#f4f7fb;color:#14213d;margin:0;padding:24px}}
            main{{max-width:560px;margin:12vh auto;background:white;border-radius:18px;padding:32px;box-shadow:0 12px 40px #14213d18}}
            h1{{font-size:28px;margin:0 0 12px}}p{{margin:0;color:#4b5563}}</style>
            <main><h1>{html.escape(title)}</h1><p>{html.escape(body)}</p></main></html>"""
            self._reply(status, document, "text/html; charset=utf-8")

        def _reply(self, status: HTTPStatus, body: str, content_type: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, fmt: str, *args: object) -> None:
            # BaseHTTPRequestHandler includes the full request target in its
            # default access log. OAuth callbacks carry a short-lived code and
            # state in the query string, so only the path is safe to record.
            safe_path = urllib.parse.urlsplit(self.path).path
            log.info("oauth-http %s %s", self.command, safe_path)

    return Handler


class LimitedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        request.settimeout(10)
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


def serve(service: BotService) -> None:
    server = LimitedHTTPServer(
        (service.config.http_host, service.config.http_port), make_handler(service)
    )
    server.serve_forever()
