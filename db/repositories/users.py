from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.enums import StartResult
from db.models import TelegramUser
from db.schemas.user import UserPage, UserRow, UserStart, UserStats


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


def _row(model: TelegramUser) -> UserRow:
    return UserRow.model_validate(model)


async def _get(session: AsyncSession, telegram_user_id: int) -> TelegramUser | None:
    return await session.scalar(
        select(TelegramUser).where(TelegramUser.telegram_user_id == telegram_user_id)
    )


async def get_user(session: AsyncSession, telegram_user_id: int) -> UserRow | None:
    model = await _get(session, telegram_user_id)
    return _row(model) if model is not None else None


async def note_seen_and_is_banned(session: AsyncSession, telegram_user_id: int) -> bool:
    """Refresh last_seen when the person is already known. Missing rows are not banned."""
    model = await _get(session, telegram_user_id)
    if model is None:
        return False
    model.last_seen_at = utcnow()
    await session.flush()
    return bool(model.is_banned)


async def register_start(session: AsyncSession, payload: UserStart) -> tuple[UserRow, StartResult]:
    """Record /start. A banned id stays banned and does not get its profile back."""
    now = utcnow()
    model = await _get(session, payload.telegram_user_id)
    if model is not None and model.is_banned:
        model.last_seen_at = now
        await session.flush()
        return _row(model), StartResult.banned
    if model is None:
        model = TelegramUser(
            telegram_user_id=payload.telegram_user_id,
            username=_clip(payload.username, 32),
            first_name=_clip(payload.first_name, 255),
            last_name=_clip(payload.last_name, 255),
            language_code=_clip(payload.language_code, 16),
            is_banned=False,
            bot_blocked=False,
            profile_cleared=False,
            started_at=now,
            last_seen_at=now,
        )
        session.add(model)
        await session.flush()
        await session.refresh(model)
        return _row(model), StartResult.created

    model.username = _clip(payload.username, 32)
    model.first_name = _clip(payload.first_name, 255)
    model.last_name = _clip(payload.last_name, 255)
    model.language_code = _clip(payload.language_code, 16)
    model.profile_cleared = False
    model.bot_blocked = False
    model.last_seen_at = now
    if model.started_at is None:
        model.started_at = now
    await session.flush()
    await session.refresh(model)
    return _row(model), StartResult.updated


async def ban_user(
    session: AsyncSession,
    telegram_user_id: int,
    reason: str | None = None,
) -> UserRow:
    """Close access. Creates a tombstone if this id has never started the bot."""
    now = utcnow()
    model = await _get(session, telegram_user_id)
    if model is None:
        model = TelegramUser(
            telegram_user_id=telegram_user_id,
            is_banned=True,
            bot_blocked=False,
            profile_cleared=False,
            started_at=None,
            last_seen_at=now,
            banned_at=now,
            ban_reason=_clip(reason, 255),
        )
        session.add(model)
    else:
        model.is_banned = True
        model.banned_at = now
        if reason is not None:
            model.ban_reason = _clip(reason, 255)
    await session.flush()
    await session.refresh(model)
    return _row(model)


async def unban_user(session: AsyncSession, telegram_user_id: int) -> UserRow | None:
    model = await _get(session, telegram_user_id)
    if model is None:
        return None
    model.is_banned = False
    model.banned_at = None
    model.ban_reason = None
    await session.flush()
    await session.refresh(model)
    return _row(model)


async def delete_user_profile(session: AsyncSession, telegram_user_id: int) -> UserRow:
    """Drop the stored name and keep the id banned so /start stays closed."""
    now = utcnow()
    model = await _get(session, telegram_user_id)
    if model is None:
        model = TelegramUser(
            telegram_user_id=telegram_user_id,
            started_at=None,
            last_seen_at=now,
        )
        session.add(model)
    model.username = None
    model.first_name = None
    model.last_name = None
    model.language_code = None
    model.profile_cleared = True
    model.is_banned = True
    model.banned_at = now
    model.ban_reason = "удаление профиля"
    await session.flush()
    await session.refresh(model)
    return _row(model)


async def mark_bot_blocked(session: AsyncSession, telegram_user_id: int, *, blocked: bool) -> None:
    """Remember that the person blocked or unblocked the bot. Unknown ids are ignored."""
    model = await _get(session, telegram_user_id)
    if model is None:
        return
    model.bot_blocked = blocked
    model.last_seen_at = utcnow()
    await session.flush()


async def list_match_recipient_ids(
    session: AsyncSession,
    *,
    extra_ids: Iterable[int] = (),
) -> list[int]:
    """Telegram ids that should receive a new intersection card.

    Includes people who pressed ``/start``, skips closed access and chats that
    blocked the bot, and adds ``extra_ids`` that have no user row.
    """
    models = (await session.scalars(select(TelegramUser))).all()
    known: set[int] = set()
    recipients: set[int] = set()
    for model in models:
        user_id = int(model.telegram_user_id)
        known.add(user_id)
        if model.started_at is None or model.is_banned or model.bot_blocked:
            continue
        recipients.add(user_id)
    for user_id in extra_ids:
        extra = int(user_id)
        if extra not in known:
            recipients.add(extra)
    return sorted(recipients)


async def user_stats(session: AsyncSession, *, now: datetime | None = None) -> UserStats:
    moment = now or utcnow()
    moment = _as_utc(moment)
    today_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = moment - timedelta(days=7)
    stats = UserStats()
    models = await session.scalars(select(TelegramUser))
    for model in models.all():
        if model.is_banned:
            stats.banned += 1
        if model.bot_blocked:
            stats.blocked_bot += 1
        if model.started_at is None:
            continue
        stats.total_started += 1
        if _as_utc(model.started_at) >= today_start:
            stats.started_today += 1
        if _as_utc(model.last_seen_at) >= week_ago:
            stats.seen_7d += 1
    return stats


async def list_users(session: AsyncSession, *, offset: int, limit: int) -> UserPage:
    models = list((await session.scalars(select(TelegramUser))).all())
    models.sort(key=lambda row: (_as_utc(row.last_seen_at), row.id), reverse=True)
    page = models[offset : offset + limit]
    return UserPage(
        items=[_row(model) for model in page],
        total=len(models),
        offset=offset,
    )
