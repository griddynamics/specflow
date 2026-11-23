"""Fernet encryption of the refresh tokens at rest."""

from __future__ import annotations

from typing import override

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr


class CipherError(Exception):
    """The ciphertext was made with another key (a rotated token key) or is damaged."""


class TokenCipher:
    def __init__(self, token_key: SecretStr) -> None:
        self._fernet = Fernet(token_key.get_secret_value().encode("ascii"))

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def encrypt(self, token: SecretStr) -> bytes:
        return self._fernet.encrypt(token.get_secret_value().encode())

    def decrypt(self, ciphertext: bytes) -> SecretStr:
        try:
            return SecretStr(self._fernet.decrypt(bytes(ciphertext)).decode())
        except InvalidToken:
            raise CipherError from None
