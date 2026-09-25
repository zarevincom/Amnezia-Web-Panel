"""Per-user activity tracking, the weekly admin digest and Telegram backup timing.

Activity is derived from the traffic deltas the background sync already
computes, so it works the same way for every protocol and needs no extra SSH
round-trips.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_DIGEST_SETTINGS = {
    'enabled': False,
    'chat_id': '',
    'weekday': 0,  # Monday
    'hour': 10,
    'timezone': 'Europe/Moscow',
    'inactive_days': 7,
}

DEFAULT_BACKUP_SETTINGS = {
    'enabled': False,
    'chat_id': '',
    'interval_hours': 24,
    'last_sent_at': None,
    'last_status': None,
    'last_error': '',
}

BACKUP_INTERVALS = (24, 168)
TELEGRAM_DOCUMENT_LIMIT = 50 * 1024 * 1024


def record_traffic(user: dict, connection: dict, delta: int, now: datetime) -> None:
    """Account one sync delta: activity timestamps and the calendar-month counter."""
    if delta <= 0:
        return
    stamp = now.isoformat()
    connection['last_active_at'] = stamp
    user['last_active_at'] = stamp
    month_key = now.strftime('%Y-%m')
    if user.get('traffic_month_key') != month_key:
        user['traffic_month_key'] = month_key
        user['traffic_month'] = 0
    user['traffic_month'] = int(user.get('traffic_month') or 0) + delta


def digest_settings(data: dict) -> dict:
    result = dict(DEFAULT_DIGEST_SETTINGS)
    result.update((data.get('settings') or {}).get('weekly_digest') or {})
    return result


def backup_settings(data: dict) -> dict:
    result = dict(DEFAULT_BACKUP_SETTINGS)
    result.update((data.get('settings') or {}).get('telegram_backup') or {})
    return result


# Moscow has had no DST since 2014; used when the host lacks tzdata.
_MOSCOW_FALLBACK = timezone(timedelta(hours=3), 'MSK')


def _zone(name: str):
    try:
        return ZoneInfo(name or 'Europe/Moscow')
    except (ZoneInfoNotFoundError, ValueError):
        return _MOSCOW_FALLBACK


def _parse(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def digest_due(cfg: dict, state: dict, now: datetime) -> bool:
    """True once per local day that matches the configured weekday and hour."""
    if not cfg.get('enabled') or not str(cfg.get('chat_id') or '').strip():
        return False
    zone = _zone(cfg.get('timezone'))
    local_now = now.astimezone(zone)
    if local_now.weekday() != int(cfg.get('weekday', 0)) or local_now.hour < int(cfg.get('hour', 10)):
        return False
    last = _parse(state.get('last_sent_at'))
    return last is None or last.astimezone(zone).date() != local_now.date()


def backup_due(cfg: dict, now: datetime) -> bool:
    if not cfg.get('enabled') or not str(cfg.get('chat_id') or '').strip():
        return False
    last = _parse(cfg.get('last_sent_at'))
    interval = int(cfg.get('interval_hours') or 24)
    return last is None or now - last >= timedelta(hours=interval)


def format_bytes(value) -> str:
    value = float(value or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024:
            return f'{int(value)} {unit}' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024
    return f'{value:.1f} TB'


def _days_since(value, now: datetime) -> Optional[int]:
    moment = _parse(value)
    if moment is None:
        return None
    return max(0, (now - moment).days)


def _ago(days: Optional[int]) -> str:
    if days is None:
        return 'нет данных'
    if days == 0:
        return 'сегодня'
    return f'{days} дн. назад'


def build_digest(data: dict, state: dict, now: datetime, inactive_days: int = 7) -> dict:
    """Return {'text', 'totals'}; totals become the baseline for next week."""
    baseline = state.get('baseline') or {}
    since = _parse(state.get('baseline_at'))
    period_start = since or (now - timedelta(days=7))
    owners = {c.get('user_id') for c in data.get('user_connections', [])}
    users = [u for u in data.get('users', []) if u.get('id') in owners]

    rows, silent, disabled, totals = [], [], [], {}
    week_total = 0
    for user in users:
        total = int(user.get('traffic_total') or 0)
        totals[user['id']] = total
        # Without a baseline the first digest reports lifetime traffic.
        week = max(0, total - int(baseline.get(user['id'], 0))) if user['id'] in baseline else total
        days = _days_since(user.get('last_active_at'), now)
        if not user.get('enabled', True):
            disabled.append(user)
            continue
        week_total += week
        rows.append((week, user, days))
        if days is None or days >= inactive_days:
            silent.append((user, days))

    rows.sort(key=lambda item: item[0], reverse=True)
    active = sum(1 for _, _, days in rows if days is not None and days < inactive_days)
    lines = [
        f'📊 Сводка VPN за неделю ({period_start.strftime("%d.%m")}–{now.strftime("%d.%m")})',
        f'Всего трафика: {format_bytes(week_total)} · активных: {active} из {len(rows)}',
    ]
    if not since:
        lines.append('Первая сводка: трафик указан за всё время.')
    if rows:
        lines.append('')
        lines += [f'👤 {user.get("username")} — {format_bytes(week)} · {_ago(days)}' for week, user, days in rows]
    if silent:
        lines += ['', f'😴 Не пользовались {inactive_days}+ дней:']
        lines += [f'• {user.get("username")} — {_ago(days)}' for user, days in silent]
    if disabled:
        lines += ['', '⛔ Отключены: ' + ', '.join(str(u.get('username')) for u in disabled)]
    if not users:
        lines += ['', 'Профили пока не назначены ни одному пользователю.']
    return {'text': '\n'.join(lines), 'totals': totals}


def backup_caption(data: dict, key_fingerprint: str, now: datetime) -> str:
    return (
        f'💾 Бэкап Amnezia Web Panel · {now.strftime("%d.%m.%Y %H:%M")} UTC\n'
        f'Серверов: {len(data.get("servers", []))} · пользователей: {len(data.get("users", []))} · '
        f'профилей: {len(data.get("user_connections", []))}\n'
        f'Мастер-ключ: {key_fingerprint or "не задан"}\n'
        'Восстановление: Settings → Restore с тем же PANEL_MASTER_KEY.'
    )
