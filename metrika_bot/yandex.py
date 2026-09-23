from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .runtime import RequestGate, QuotaWait

from .config import Config
from .crypto import TokenCipher
from .db import Database


UTC = timezone.utc
AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
TOKEN_URL = "https://oauth.yandex.ru/token"
METRIKA_API = "https://api-metrika.yandex.net"


class YandexAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: int = 0,
        reconnect: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.reconnect = reconnect


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str | None
    expires_at: str | None


class YandexClient:
    def __init__(self, config: Config, db: Database, cipher: TokenCipher):
        self.config = config
        self.db = db
        self.cipher = cipher
        self.gate = RequestGate()
        self.token_lock = threading.RLock()
        self.report_local = threading.local()

    @contextmanager
    def report_scope(self, chat_id: int, generation: str):
        self.report_local.scope = (chat_id, generation, time.monotonic() + 180)
        try:
            yield
        finally:
            self.report_local.scope = None

    def check_report_scope(self):
        scope = getattr(self.report_local, "scope", None)
        if scope:
            chat_id, generation, deadline = scope
            row = self.db.get_connection(chat_id)
            if not row or row["generation"] != generation:
                raise YandexAPIError("Подключение изменилось. Откройте новый отчёт.")
            if time.monotonic() >= deadline:
                raise YandexAPIError(
                    "Метрика отвечает слишком долго. Повторите отчёт позже.", retry_after=300
                )

    def authorization_url(self, chat_id: int) -> str:
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        self.db.save_oauth_state(state, chat_id, verifier)
        params = {
            "response_type": "code",
            "client_id": self.config.yandex_client_id,
            "redirect_uri": self.config.yandex_redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "force_confirm": "yes",
            "scope": "metrika:read",
        }
        return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)

    def exchange_code(self, code: str, verifier: str) -> OAuthTokens:
        return self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.config.yandex_client_id,
                "client_secret": self.config.yandex_client_secret,
                "redirect_uri": self.config.yandex_redirect_uri,
                "code_verifier": verifier,
            }
        )

    def _refresh(self, refresh_token: str) -> OAuthTokens:
        return self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.config.yandex_client_id,
                "client_secret": self.config.yandex_client_secret,
            }
        )

    def _token_request(self, fields: dict[str, str]) -> OAuthTokens:
        request = urllib.request.Request(
            TOKEN_URL,
            data=urllib.parse.urlencode(fields).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        payload = self._open_json(request)
        access = payload.get("access_token")
        if not access:
            raise YandexAPIError("Яндекс не вернул access_token")
        expires_at = None
        if payload.get("expires_in"):
            expires_at = (
                datetime.now(UTC) + timedelta(seconds=int(payload["expires_in"]))
            ).isoformat()
        return OAuthTokens(access, payload.get("refresh_token"), expires_at)

    def token_for(self, chat_id: int) -> str:
        with self.token_lock:
            return self._token_for(chat_id)

    def _token_for(self, chat_id: int) -> str:
        row = self.db.get_connection(chat_id)
        if not row:
            raise YandexAPIError("Метрика ещё не подключена")
        access = self.cipher.decrypt(row["access_token"])
        refresh = self.cipher.decrypt(row["refresh_token"])
        expires_at = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
        if expires_at and expires_at <= datetime.now(UTC) + timedelta(minutes=5):
            if not refresh:
                raise YandexAPIError(
                    "Доступ к Метрике истёк — подключите её заново", reconnect=True
                )
            tokens = self._refresh(refresh)
            refresh = tokens.refresh_token or refresh
            updated = self.db.update_tokens(
                chat_id,
                self.cipher.encrypt(tokens.access_token) or "",
                self.cipher.encrypt(refresh),
                tokens.expires_at,
                expected_generation=row["generation"],
            )
            if not updated:
                raise YandexAPIError("Подключение изменилось. Откройте новый отчёт.")
            access = tokens.access_token
        if not access:
            raise YandexAPIError("Не удалось расшифровать доступ к Метрике")
        return access

    def save_tokens(self, chat_id: int, tokens: OAuthTokens) -> None:
        self.db.save_tokens(
            chat_id,
            self.cipher.encrypt(tokens.access_token) or "",
            self.cipher.encrypt(tokens.refresh_token),
            tokens.expires_at,
        )

    def counters(self, chat_id: int) -> list[dict[str, Any]]:
        counters = []
        for page in range(1, 101):
            data = self._api(
                chat_id,
                "/management/v1/counters",
                {"per_page": 1000, "offset": (page - 1) * 1000 + 1},
            )
            batch = data.get("counters", [])
            counters.extend(batch)
            if len(batch) < 1000:
                break
        return counters

    def goals(self, chat_id: int, counter_id: int) -> list[dict[str, Any]]:
        data = self._api(chat_id, f"/management/v1/counter/{counter_id}/goals")
        return data.get("goals", [])

    def report(
        self,
        chat_id: int,
        counter_id: int,
        date1: str,
        date2: str,
        metrics: list[str],
        dimensions: list[str] | None = None,
        limit: int = 100,
        filters: str | None = None,
        offset: int = 1,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "ids": counter_id,
            "metrics": ",".join(metrics),
            "date1": date1,
            "date2": date2,
            "limit": limit,
            "accuracy": "full",
            "lang": "ru",
            "offset": offset,
            "timezone": self.report_timezone(date2),
            "filters": "ym:s:isRobot=='No'",
        }
        if dimensions:
            params["dimensions"] = ",".join(dimensions)
        if filters:
            params["filters"] = f"({filters}) AND ym:s:isRobot=='No'"
        return self._api(chat_id, "/stat/v1/data", params)

    def report_timezone(self, day: str) -> str:
        zone = ZoneInfo(self.config.report_timezone)
        delta = datetime.fromisoformat(day).replace(tzinfo=zone).utcoffset()
        minutes = int(delta.total_seconds() // 60)
        return f"{'+' if minutes >= 0 else '-'}{abs(minutes) // 60:02}:{abs(minutes) % 60:02}"

    def _api(self, chat_id: int, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.check_report_scope()
        query = urllib.parse.urlencode(params or {})
        url = METRIKA_API + path + ("?" + query if query else "")
        access = self.token_for(chat_id)
        key = hashlib.sha256(access.encode()).hexdigest()
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": "OAuth " + access,
                "Accept": "application/json",
                "User-Agent": "PrivateSEO-Metrika-Bot/0.3",
            },
        )
        for attempt in range(3):
            try:
                with self.gate.enter(key, path.startswith("/stat/")):
                    self.check_report_scope()
                    return self._open_json(request)
            except QuotaWait as exc:
                raise YandexAPIError(
                    "Метрика временно ограничила частоту отчётов. Повторите позже.",
                    retry_after=exc.retry_after,
                ) from None
            except YandexAPIError as exc:
                if exc.status not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise
                delay = max(exc.retry_after, 2 ** (attempt + 1))
                if delay > 10:
                    raise
                time.sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    def _open_json(request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                body = {}
            try:
                delay = int(exc.headers.get("Retry-After", "0"))
            except (ValueError, AttributeError):
                delay = 0
            reauth = exc.code in {401, 403} or body.get("error") == "invalid_grant"
            if reauth:
                message = (
                    "Доступ к Метрике отозван или недостаточен. Подключите аккаунт заново: /connect"
                )
            elif exc.code == 429:
                message = "Метрика временно ограничила частоту запросов. Попробуйте позже."
                delay = max(delay, 300)
            elif exc.code == 400:
                message = "Метрика не приняла параметры отчёта. Проверьте выбранные счётчик и цели."
            else:
                message = "Метрика временно недоступна. Попробуйте позже."
            raise YandexAPIError(
                message, status=exc.code, retry_after=delay, reconnect=reauth
            ) from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
            raise YandexAPIError("Яндекс API временно недоступен", retry_after=300) from None
