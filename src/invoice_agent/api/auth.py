"""Who may decide an invoice, and proof that a form was meant by the person who sent it.

Approvers are listed in the environment with a scrypt hash of their password (the standard
library, no extra dependency). The web layer uses HTTP Basic, so every request is
authenticated and no session has to be stored; because a browser sends those credentials by
itself, a form that changes something also carries a CSRF token that only the logged-in
approver can have been given.
"""

import base64
import hashlib
import hmac
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

NAME = re.compile(r"[A-Za-z0-9._@-]{1,100}")
SCRYPT_N, SCRYPT_R, SCRYPT_P, KEY_LENGTH = 2**14, 8, 1, 32
MIN_SECRET_LENGTH = 32


class AuthConfigError(ValueError):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """``scrypt$N$r$p$salt$hash``: everything needed to check the password later."""
    salt = salt if salt is not None else os.urandom(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_LENGTH
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=KEY_LENGTH
        )
        return hmac.compare_digest(digest, _unb64(expected))
    except (ValueError, TypeError):
        return False


# Checked against when the user is unknown, so that an unknown name costs as much time as a
# wrong password and the response time does not tell which names exist.
_UNKNOWN_USER_HASH = hash_password("no such user", salt=b"\x00" * 16)


@dataclass(frozen=True)
class Approvers:
    hashes: Mapping[str, str]

    @classmethod
    def parse(cls, value: str) -> "Approvers":
        """``alice=scrypt$...,bob=scrypt$...``"""
        hashes: dict[str, str] = {}
        for entry in filter(None, (part.strip() for part in value.split(","))):
            name, separator, stored = entry.partition("=")
            if not separator or not NAME.fullmatch(name):
                raise AuthConfigError("APPROVERS entries look like name=scrypt$...")
            if stored.count("$") != 5 or not stored.startswith("scrypt$"):
                raise AuthConfigError(f"the password hash of {name!r} is not a scrypt hash")
            if name in hashes:
                raise AuthConfigError(f"approver {name!r} is listed twice")
            hashes[name] = stored
        if not hashes:
            raise AuthConfigError("APPROVERS lists nobody")
        return cls(hashes)

    def authenticate(self, name: str, password: str) -> bool:
        stored = self.hashes.get(name)
        ok = verify_password(password, stored if stored is not None else _UNKNOWN_USER_HASH)
        return ok and stored is not None


def csrf_token(secret: bytes, user: str, invoice_id: UUID) -> str:
    """A token for one approver and one invoice: no state to store, and not guessable."""
    message = f"csrf:{user}:{invoice_id}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def csrf_valid(secret: bytes, user: str, invoice_id: UUID, token: str) -> bool:
    return hmac.compare_digest(csrf_token(secret, user, invoice_id), token)
