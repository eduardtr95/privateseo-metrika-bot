import pytest
from cryptography.fernet import Fernet

from metrika_bot.crypto import TokenCipher


def test_token_round_trip_and_no_plaintext():
    cipher = TokenCipher(Fernet.generate_key().decode())
    encrypted = cipher.encrypt("secret-token")
    assert encrypted != "secret-token"
    assert "secret-token" not in encrypted
    assert cipher.decrypt(encrypted) == "secret-token"


def test_invalid_key_is_rejected():
    with pytest.raises(RuntimeError):
        TokenCipher("not-a-fernet-key")
