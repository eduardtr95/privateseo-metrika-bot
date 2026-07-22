import urllib.parse
from pathlib import Path

from cryptography.fernet import Fernet

from metrika_bot.config import Config
from metrika_bot.crypto import TokenCipher
from metrika_bot.db import Database
from metrika_bot.yandex import YandexClient


def config(tmp_path: Path) -> Config:
    return Config(
        telegram_bot_token="telegram",
        yandex_client_id="client",
        yandex_client_secret="secret",
        yandex_redirect_uri="https://example.test/oauth/callback",
        token_encryption_key=Fernet.generate_key().decode(),
        database_path=tmp_path / "db.sqlite3",
    )


def test_authorization_url_uses_code_flow_pkce_and_one_time_state(tmp_path: Path):
    cfg = config(tmp_path)
    db = Database(cfg.database_path)
    db.upsert_user(123, "user")
    client = YandexClient(cfg, db, TokenCipher(cfg.token_encryption_key))
    url = urllib.parse.urlparse(client.authorization_url(123))
    query = urllib.parse.parse_qs(url.query)
    assert url.netloc == "oauth.yandex.ru"
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert "code_challenge" in query
    assert db.consume_oauth_state(query["state"][0])["chat_id"] == 123


def test_stored_access_token_is_encrypted(tmp_path: Path):
    cfg = config(tmp_path)
    db = Database(cfg.database_path)
    db.upsert_user(123, "user")
    cipher = TokenCipher(cfg.token_encryption_key)
    client = YandexClient(cfg, db, cipher)
    from metrika_bot.yandex import OAuthTokens

    client.save_tokens(123, OAuthTokens("plain-access", "plain-refresh", None))
    row = db.get_connection(123)
    assert "plain-access" not in row["access_token"]
    assert cipher.decrypt(row["access_token"]) == "plain-access"
