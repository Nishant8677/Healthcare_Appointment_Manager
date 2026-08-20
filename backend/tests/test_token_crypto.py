"""Refresh-token encryption.

Small surface, high stakes: the failure this guards against is a database dump handing an
attacker working access to patients' and doctors' calendars.
"""

from __future__ import annotations

import pytest

from app.services.token_crypto import TokenCipher, TokenEncryptionError

OTHER_KEY = "3-1KJTZOtnLtEGWzHy9eMK2ZOOL_xrQtjc8m4Xw0KEE="


def test_round_trips_a_token(token_cipher: TokenCipher) -> None:
    assert token_cipher.decrypt(token_cipher.encrypt("1//refresh-token")) == "1//refresh-token"


def test_ciphertext_does_not_contain_the_plaintext(token_cipher: TokenCipher) -> None:
    """The point of the whole module: a dump of this column must not reveal the token.

    Worth asserting rather than assuming — an "encrypt" that quietly base64-encoded would
    round-trip perfectly and pass every other test here.
    """
    secret = "1//0gTHIS-IS-THE-SECRET-PART"
    ciphertext = token_cipher.encrypt(secret)
    assert secret not in ciphertext


def test_the_same_token_encrypts_differently_each_time(token_cipher: TokenCipher) -> None:
    """Fernet uses a random IV, so identical tokens do not produce identical rows.

    Without this, two users who somehow held the same token would be visibly linked in the
    table, and repeated values would leak structure to anyone reading it.
    """
    first = token_cipher.encrypt("same-token")
    second = token_cipher.encrypt("same-token")
    assert first != second
    assert token_cipher.decrypt(first) == token_cipher.decrypt(second) == "same-token"


def test_a_different_key_cannot_decrypt(token_cipher: TokenCipher) -> None:
    ciphertext = token_cipher.encrypt("1//refresh-token")
    with pytest.raises(TokenEncryptionError, match="could not be decrypted"):
        TokenCipher(OTHER_KEY).decrypt(ciphertext)


def test_tampered_ciphertext_is_rejected_rather_than_decrypted(token_cipher: TokenCipher) -> None:
    """Fernet authenticates, so an edited row fails loudly instead of yielding nonsense."""
    ciphertext = token_cipher.encrypt("1//refresh-token")
    tampered = ciphertext[:-4] + ("AAAA" if not ciphertext.endswith("AAAA") else "BBBB")
    with pytest.raises(TokenEncryptionError):
        token_cipher.decrypt(tampered)


def test_an_invalid_key_is_reported_with_the_command_to_generate_one() -> None:
    """The error a developer actually hits should tell them how to fix it."""
    with pytest.raises(TokenEncryptionError, match=r"Fernet\.generate_key"):
        TokenCipher("not-a-real-key")


def test_the_error_message_never_carries_the_ciphertext(token_cipher: TokenCipher) -> None:
    """A message that echoes the encrypted token would put it straight into the logs."""
    ciphertext = token_cipher.encrypt("1//refresh-token")
    with pytest.raises(TokenEncryptionError) as caught:
        TokenCipher(OTHER_KEY).decrypt(ciphertext)
    assert ciphertext not in str(caught.value)
