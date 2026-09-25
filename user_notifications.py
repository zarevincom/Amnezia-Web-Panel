"""Deliver newly issued or transferred VPN profiles to their owners in Telegram.

The routes that create profiles only decide *whether* a user should be told
and hand the actual Telegram delivery to the event loop, so a slow or failing
Bot API never delays or breaks profile creation.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

KIND_CREATED = 'created'
KIND_ASSIGNED = 'assigned'
KIND_TRANSFERRED = 'transferred'

# Result codes returned to the UI (`user_notification` in API responses).
QUEUED = 'queued'
DISABLED = 'disabled'
NO_USER = 'no_user'
NO_TELEGRAM = 'no_telegram'
NO_BOT = 'no_bot'

DEFAULT_SETTINGS = {
    'enabled': True,
    'on_create': True,
    'on_transfer': True,
}

_main_loop: Optional[asyncio.AbstractEventLoop] = None


def settings(data: dict) -> dict:
    result = dict(DEFAULT_SETTINGS)
    result.update((data.get('settings') or {}).get('user_notifications') or {})
    return result


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Remember the server loop so threadpool routes can schedule deliveries."""
    global _main_loop
    _main_loop = loop


def resolve_recipient(data: dict, user_id: Optional[str], kind: str, requested: bool = True):
    """Return (status, user). status is QUEUED when a message should be sent."""
    cfg = settings(data)
    event_enabled = cfg.get('on_transfer') if kind == KIND_TRANSFERRED else cfg.get('on_create')
    if not requested or not cfg.get('enabled') or not event_enabled:
        return DISABLED, None
    user = next((u for u in data.get('users', []) if u.get('id') == user_id), None) if user_id else None
    if not user or not user.get('enabled', True):
        return NO_USER, None
    if not str(user.get('telegramId') or '').strip():
        return NO_TELEGRAM, user
    if not (data.get('settings') or {}).get('telegram', {}).get('token'):
        return NO_BOT, user
    return QUEUED, user


def schedule(coro_factory: Callable[[], Awaitable]) -> bool:
    """Run a delivery coroutine in the background from sync or async code."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(_guarded(coro_factory))
        return True
    if _main_loop is not None and _main_loop.is_running():
        asyncio.run_coroutine_threadsafe(_guarded(coro_factory), _main_loop)
        return True
    logger.warning('User notification dropped: no running event loop')
    return False


async def _guarded(coro_factory: Callable[[], Awaitable]):
    try:
        await coro_factory()
    except Exception:
        logger.exception('User profile notification failed')


def record_result(data: dict, error: Optional[str]) -> None:
    """Keep the last delivery outcome visible on the notifications page."""
    cfg = data.setdefault('settings', {}).setdefault('user_notifications', dict(DEFAULT_SETTINGS))
    cfg['last_attempt_at'] = datetime.now(timezone.utc).isoformat()
    cfg['last_error'] = error or ''
