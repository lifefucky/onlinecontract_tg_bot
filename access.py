"""Stop banned people from using the bot. Owners listed in ADMIN_IDS stay allowed."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from db.repositories import users
from misc.admins import get_admin_ids

ACCESS_CLOSED = "доступ закрыт"


def should_deny_access(*, is_banned: bool, is_admin: bool) -> bool:
    return is_banned and not is_admin


def _user_id(event: TelegramObject) -> int | None:
    user = getattr(event, "from_user", None)
    if user is None:
        return None
    return int(user.id)


async def _reply_closed(event: TelegramObject) -> None:
    try:
        if isinstance(event, CallbackQuery):
            await event.answer(ACCESS_CLOSED, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(ACCESS_CLOSED)
    except Exception:
        logging.debug("access closed reply failed", exc_info=True)


class AccessMiddleware(BaseMiddleware):
    """Runs inside the DB session middleware. my_chat_member is always delivered."""

    async def __call__(
        self,
        handler: Any,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, ChatMemberUpdated):
            return await handler(event, data)
        uid = _user_id(event)
        session: AsyncSession | None = data.get("session")
        if uid is None or session is None:
            return await handler(event, data)
        banned = await users.note_seen_and_is_banned(session, uid)
        if should_deny_access(is_banned=banned, is_admin=uid in get_admin_ids()):
            await _reply_closed(event)
            return None
        return await handler(event, data)
