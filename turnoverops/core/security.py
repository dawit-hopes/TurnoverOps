import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import jwt

from turnoverops.core.config import Settings
from turnoverops.core.errors import AppError


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def secrets_equal(left: str, right: str) -> bool:
    left_bytes = left.encode()
    right_bytes = right.encode()
    if len(left_bytes) != len(right_bytes):
        return False
    return hmac.compare_digest(left_bytes, right_bytes)


def create_token(
    subject: str,
    settings: Settings,
    *,
    purpose: str,
    ttl_minutes: int,
    extra: dict | None = None,
) -> str:
    now = datetime.now(UTC)
    payload = dict(extra or {})
    payload.update(
        {
            "sub": subject,
            "purpose": purpose,
            "iat": now,
            "exp": now + timedelta(minutes=ttl_minutes),
        }
    )
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str, settings: Settings, *, purpose: str) -> dict:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise AppError(401, "unauthorized", "Invalid session") from exc
    if payload.get("purpose") != purpose:
        raise AppError(401, "unauthorized", "Invalid session")
    return payload
