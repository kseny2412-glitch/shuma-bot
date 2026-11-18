"""Одноразовые коды для простой электронной подписи (ПЭП)."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import Database

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5


def _hash(tg_id: int, code: str) -> str:
    return hashlib.sha256(f"{tg_id}:{code}".encode()).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def issue_code(db: Database, tg_id: int) -> str:
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires = (datetime.now(timezone.utc) + CODE_TTL).isoformat(timespec="seconds")
    db.put_code(tg_id, _hash(tg_id, code), expires)
    return code


@dataclass
class CheckResult:
    ok: bool
    reason: str = ""   # "", "missing", "expired", "too_many", "wrong"


def check_code(db: Database, tg_id: int, code: str) -> CheckResult:
    row = db.get_code(tg_id)
    if not row:
        return CheckResult(False, "missing")
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        db.drop_code(tg_id)
        return CheckResult(False, "expired")
    if row["attempts"] >= MAX_ATTEMPTS:
        db.drop_code(tg_id)
        return CheckResult(False, "too_many")
    if not hmac.compare_digest(row["code_hash"], _hash(tg_id, code.strip())):
        db.bump_attempts(tg_id)
        return CheckResult(False, "wrong")
    db.drop_code(tg_id)
    return CheckResult(True)
