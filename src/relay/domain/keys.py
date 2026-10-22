"""Gateway API keys. Only a SHA-256 of the key is stored; the plaintext is shown once."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

KEY_PREFIX = "rk_"
DISPLAY_PREFIX_LEN = 10


@dataclass(frozen=True, slots=True)
class ApiKey:
    id: UUID
    name: str
    owner: str
    prefix: str
    rpm_limit: int
    tpd_limit: int
    monthly_budget_usd: Decimal | None
    enforce_budget: bool
    allowed_models: tuple[str, ...] | None
    created_at: datetime
    revoked_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def may_use(self, model: str) -> bool:
        return self.allowed_models is None or model in self.allowed_models


@dataclass(frozen=True, slots=True)
class NewKey:
    plaintext: str
    key_hash: str
    prefix: str


def generate_key() -> NewKey:
    plaintext = KEY_PREFIX + secrets.token_urlsafe(32)
    return NewKey(
        plaintext=plaintext, key_hash=hash_key(plaintext), prefix=display_prefix(plaintext)
    )


def hash_key(plaintext: str) -> str:
    # API keys are 256-bit random strings, so a fast hash is enough: there is nothing to
    # brute-force, unlike passwords, and lookups stay O(1) on an indexed column.
    return hashlib.sha256(plaintext.encode()).hexdigest()


def display_prefix(plaintext: str) -> str:
    return plaintext[:DISPLAY_PREFIX_LEN]
