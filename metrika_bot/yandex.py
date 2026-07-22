from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Config
from .crypto import TokenCipher
from .db import Database


UTC = timezone.utc
AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
TOKEN_URL = "https://oauth.yandex.ru/token"
METRIKA_API = "https://api-metrika.yandex.net"


class YandexAPIError(RuntimeError):
    pass


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
        row = self.db.get_connection(chat_id)
        if not row:
            raise YandexAPIError("Метрика ещё не подключена")
        access = self.cipher.decrypt(row["access_token"])
        refresh = self.cipher.decrypt(row["refresh_token"])
        expires_at = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
        if expires_at and expires_at <= datetime.now(UTC) + timedelta(minutes=5):
            if not refresh:
                raise YandexAPIError("Доступ к Метрике истёк — подключите её заново")
            tokens = self._refresh(refresh)
            refresh = tokens.refresh_token or refresh
            self.db.update_tokens(
                chat_id,
                self.cipher.encrypt(tokens.access_token) or "",
                self.cipher.encrypt(refresh),
                tokens.expires_at,
            )
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
        data = self._api(chat_id, "/management/v1/counters", {"per_page": 1000})
        return data.get("counters", [])

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
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "ids": counter_id,
            "metrics": ",".join(metrics),
            "date1": date1,
            "date2": date2,
            "limit": limit,
            "accuracy": "full",
            "lang": "ru",
        }
        if dimensions:
            params["dimensions"] = ",".join(dimensions)
        return self._api(chat_id, "/stat/v1/data", params)

    def _api(self, chat_id: int, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = urllib.parse.urlencode(params or {})
        url = METRIKA_API + path + ("?" + query if query else "")
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": "OAuth " + self.token_for(chat_id),
                "Accept": "application/json",
                "User-Agent": "PrivateSEO-Metrika-Bot/0.1",
            },
        )
        return self._open_json(request)

    @staticmethod
    def _open_json(request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            try:
                detail = json.loads(body).get("message") or json.loads(body).get(
                    "error_description"
                )
            except (json.JSONDecodeError, AttributeError):
                detail = body
            raise YandexAPIError(f"Яндекс API: {exc.code} — {detail or 'ошибка запроса'}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise YandexAPIError("Яндекс API временно недоступен") from exc
