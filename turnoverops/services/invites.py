import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from turnoverops.core.config import Settings
from turnoverops.core.errors import AppError
from turnoverops.core.security import hash_token
from turnoverops.models import Cleaner, TelegramInvite
from turnoverops.services.audit import audit


def create_invite(db: Session, cleaner: Cleaner, settings: Settings, now: datetime | None = None) -> tuple[str, datetime]:
    moment = now or datetime.now(UTC)
    active = db.scalars(
        select(TelegramInvite).where(
            TelegramInvite.cleaner_id == cleaner.id,
            TelegramInvite.used_at.is_(None),
            TelegramInvite.revoked_at.is_(None),
        )
    ).all()
    for invite in active:
        invite.revoked_at = moment
    token = secrets.token_urlsafe(32)
    expires_at = moment + timedelta(hours=settings.invite_ttl_hours)
    db.add(
        TelegramInvite(
            host_id=cleaner.host_id,
            cleaner_id=cleaner.id,
            token_hash=hash_token(token),
            expires_at=expires_at,
        )
    )
    if cleaner.telegram_status != "connected":
        cleaner.telegram_status = "pending"
    username = (settings.telegram_bot_username or "TurnoverOpsBot").lstrip("@")
    audit(
        db,
        host_id=cleaner.host_id,
        event_type="invite_created",
        entity_type="cleaner",
        entity_id=cleaner.id,
        reason="Telegram invite created",
    )
    return f"https://t.me/{username}?start={token}", expires_at


def revoke_invites(db: Session, cleaner: Cleaner, now: datetime | None = None) -> None:
    moment = now or datetime.now(UTC)
    active = db.scalars(
        select(TelegramInvite).where(
            TelegramInvite.cleaner_id == cleaner.id,
            TelegramInvite.used_at.is_(None),
            TelegramInvite.revoked_at.is_(None),
        )
    ).all()
    for invite in active:
        invite.revoked_at = moment
    if cleaner.telegram_status == "pending":
        cleaner.telegram_status = "disconnected"
    audit(
        db,
        host_id=cleaner.host_id,
        event_type="invite_revoked",
        entity_type="cleaner",
        entity_id=cleaner.id,
        reason="Telegram invites revoked",
    )


def connect_telegram(db: Session, token: str, telegram_user_id: str, chat_id: str, now: datetime) -> Cleaner:
    invite = db.scalar(select(TelegramInvite).where(TelegramInvite.token_hash == hash_token(token)))
    if invite is None or invite.revoked_at is not None or invite.used_at is not None or invite.expires_at <= now:
        raise AppError(400, "invalid_invite", "Invite is invalid or expired")
    cleaner = db.get(Cleaner, invite.cleaner_id)
    if cleaner is None or not cleaner.active:
        raise AppError(400, "invalid_invite", "Invite is invalid or expired")
    invite.used_at = now
    cleaner.telegram_user_id = str(telegram_user_id)
    cleaner.telegram_chat_id = str(chat_id)
    cleaner.telegram_status = "connected"
    audit(
        db,
        host_id=cleaner.host_id,
        event_type="telegram_connected",
        entity_type="cleaner",
        entity_id=cleaner.id,
        reason="Telegram account connected",
    )
    return cleaner
