"""Shared, persistence-agnostic helpers for Telegram account invitations."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any


TELEGRAM_INVITE_PREFIX = "tg_"


class TelegramInviteError(ValueError):
    """Raised when a Telegram user or invitation cannot be created safely."""


def telegram_invite_hash(payload: str) -> str:
    """Return the only representation of an invitation persisted in state."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def telegram_invite_is_active(invite: dict[str, Any], now: datetime | None = None) -> bool:
    """Accept non-expiring invitations while retaining legacy expiry support."""
    if not invite.get("enabled", True) or invite.get("accepted_at"):
        return False

    expires_at = invite.get("expires_at")
    if not expires_at:
        return True
    try:
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry > (now or datetime.now(timezone.utc))


def create_telegram_user(data: dict[str, Any], username: str, created_by: str) -> dict[str, Any]:
    """Create a Telegram-owned user with the same data access as ``user``."""
    clean_name = str(username or "").strip()
    if not clean_name or len(clean_name) > 80:
        raise TelegramInviteError("invalid_username")
    if any(item.get("username") == clean_name for item in data.get("users", [])):
        raise TelegramInviteError("username_taken")

    now = datetime.now(timezone.utc).isoformat()
    user = {
        "id": str(uuid.uuid4()),
        "username": clean_name,
        "password_hash": None,
        # Telegram authenticates the recipient through the one-time deep link,
        # so this user type deliberately does not need a panel password.
        "role": "tg_user",
        "auth_source": "telegram",
        "telegramId": None,
        "email": None,
        "description": None,
        "traffic_limit": 0,
        "traffic_reset_strategy": "never",
        "traffic_used": 0,
        "traffic_total": 0,
        "last_reset_at": now,
        "expiration_date": None,
        "enabled": True,
        "created_at": now,
        "remnawave_uuid": None,
        "share_enabled": False,
        "share_token": secrets.token_urlsafe(16),
        "share_password_hash": None,
    }
    data.setdefault("users", []).append(user)
    data.setdefault("audit_log", []).append({
        "id": str(uuid.uuid4()),
        "event": "telegram_user_created",
        "created_at": now,
        "user_id": user["id"],
        "created_by": created_by,
    })
    return user


def create_telegram_invite(
    data: dict[str, Any],
    user_id: str,
    created_by: str,
) -> tuple[dict[str, Any], str]:
    """Issue one non-expiring, single-use deep link for an existing user."""
    user = next((item for item in data.get("users", []) if item.get("id") == user_id), None)
    if not user:
        raise TelegramInviteError("user_not_found")
    if not user.get("enabled", True):
        raise TelegramInviteError("user_disabled")
    if user.get("telegramId"):
        raise TelegramInviteError("telegram_already_linked")

    now = datetime.now(timezone.utc).isoformat()
    for invite in data.get("telegram_invites", []):
        if invite.get("user_id") == user_id and telegram_invite_is_active(invite):
            invite["enabled"] = False
            invite["revoked_at"] = now
            invite["revoked_by"] = created_by

    raw_payload = TELEGRAM_INVITE_PREFIX + secrets.token_urlsafe(24)
    invite = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "token_hash": telegram_invite_hash(raw_payload),
        "enabled": True,
        "created_at": now,
        "expires_at": None,
        "created_by": created_by,
        "accepted_at": None,
        "telegram_id": None,
    }
    data.setdefault("telegram_invites", []).append(invite)
    data.setdefault("audit_log", []).append({
        "id": str(uuid.uuid4()),
        "event": "telegram_invite_created",
        "created_at": now,
        "user_id": user_id,
        "invite_id": invite["id"],
        "created_by": created_by,
    })
    return invite, raw_payload
