"""Encryption for OAuth refresh tokens at rest.

A refresh token is not like a password hash. A leaked hash is a puzzle an attacker still has
to solve; a leaked refresh token is *working access to somebody's calendar*, redeemable
immediately and valid until they happen to notice and revoke it. So it is encrypted before it
reaches a column, and the key lives in the environment rather than the database — a stolen
dump is then useless on its own.

Fernet (AES-128-CBC with an HMAC-SHA256 authentication tag) via `cryptography`, rather than
anything assembled here. The alternative to one vetted dependency is hand-rolled cipher code,
which is strictly worse: this is the one place in the codebase where "write it yourself"
is the unsafe option.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class TokenEncryptionError(Exception):
    """A token could not be encrypted or decrypted.

    Almost always a rotated or mistyped `CALENDAR_TOKEN_KEY`. Raised rather than returning
    `None` so the failure surfaces as "this connection is unusable, reconnect" instead of
    being mistaken for "this user never connected".
    """


class TokenCipher:
    """Encrypts and decrypts refresh tokens with a key held in configuration."""

    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise TokenEncryptionError(
                "CALENDAR_TOKEN_KEY is not a valid Fernet key. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """Recover a stored token.

        Raises:
            TokenEncryptionError: if the key is wrong or the stored value was tampered with.
                Fernet authenticates the ciphertext, so a modified row is rejected rather
                than decrypted into silent nonsense.
        """
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError) as exc:
            # The message deliberately carries no part of the ciphertext or key.
            raise TokenEncryptionError(
                "stored calendar token could not be decrypted; the encryption key may have "
                "changed. The user needs to reconnect their calendar."
            ) from exc
