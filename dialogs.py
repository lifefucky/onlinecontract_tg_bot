"""Clear a private dialog: tracked bot messages and the pinned welcome."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.enums import MessageTrackScope
from db.repositories import kv, message_tracks
from db.repositories.kv import DIALOG_RESET_ALL_KEY

DIALOG_SCOPES = (
    MessageTrackScope.procedures,
    MessageTrackScope.positions,
    MessageTrackScope.welcome,
)


async def drop_dialog_tracks(session: AsyncSession, telegram_chat_id: int) -> list[int]:
    """Remove tracked message ids for one chat and return them for Telegram deletion."""
    ids: list[int] = []
    for scope in DIALOG_SCOPES:
        ids.extend(
            await message_tracks.delete_tracks_for_scope(
                session, telegram_chat_id=telegram_chat_id, scope=scope
            )
        )
    return ids


async def delete_messages(bot: Bot, telegram_chat_id: int, message_ids: list[int]) -> None:
    """Delete messages. Telegram rejects deletes older than 48 hours."""
    for mid in message_ids:
        try:
            await bot.delete_message(telegram_chat_id, mid)
        except Exception:
            logging.debug(
                "delete_message skipped chat=%s message=%s",
                telegram_chat_id,
                mid,
                exc_info=True,
            )
        await asyncio.sleep(0.03)


async def erase_tracked_messages(bot: Bot, telegram_chat_id: int, message_ids: list[int]) -> None:
    """Delete bot messages and unpin the welcome. Used when the whole dialog is dropped."""
    await delete_messages(bot, telegram_chat_id, message_ids)
    try:
        await bot.unpin_all_chat_messages(telegram_chat_id)
    except Exception:
        logging.debug("unpin_all_chat_messages skipped chat=%s", telegram_chat_id, exc_info=True)


async def interrupt_dialog(bot: Bot, session: AsyncSession, telegram_chat_id: int) -> None:
    message_ids = await drop_dialog_tracks(session, telegram_chat_id)
    await erase_tracked_messages(bot, telegram_chat_id, message_ids)


async def interrupt_all_dialogs_once(
    bot: Bot,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """On the first start after this version, clear every known chat. Later starts do nothing."""
    async with session_maker() as session:
        if await kv.get_bool(session, DIALOG_RESET_ALL_KEY):
            return
        snapshot = await message_tracks.snapshot_dialogs(session)

    logging.info("startup dialog reset for %s chats", len(snapshot))
    for chat_id, message_ids in snapshot.items():
        await erase_tracked_messages(bot, chat_id, message_ids)

    async with session_maker() as session:
        await message_tracks.delete_all_tracks(session)
        await kv.set_bool(session, DIALOG_RESET_ALL_KEY, True)
        await session.commit()
