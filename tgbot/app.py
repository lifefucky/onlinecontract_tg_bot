"""Process wiring: Bot, Dispatcher, middleware, and the background jobs."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from db import init_db_schema
from db.session import get_session_maker
from jobs.backups import backup_loop
from jobs.intersection_feed import match_poll_loop
from misc import TgKeys
from misc.admins import get_admin_ids
from misc.db_url import get_database_url, is_in_memory_sqlite
from tgbot.admin import install_command_menu, register_admin_handlers
from tgbot.dialogs import interrupt_all_dialogs_once
from tgbot.middleware import AccessMiddleware, DbSessionMiddleware
from tgbot.user import register_user_handlers

bot = Bot(token=TgKeys.TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

logging.basicConfig(level=logging.INFO)

dp.update.middleware(DbSessionMiddleware())
dp.update.middleware(AccessMiddleware())
register_admin_handlers(dp)
register_user_handlers(dp)


async def main() -> None:
    await init_db_schema()
    # Alembic's fileConfig leaves the root logger at WARN and disables loggers created earlier.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)-5.5s [%(name)s] %(message)s",
        force=True,
    )
    for existing in logging.root.manager.loggerDict.values():
        if isinstance(existing, logging.Logger):
            existing.disabled = False
    if is_in_memory_sqlite(get_database_url()):
        logging.warning(
            "DATABASE_URL is in-memory; the user journal is lost on restart. "
            "Set DATABASE_URL=sqlite+aiosqlite:///./data/app.db"
        )
    admin_ids = get_admin_ids()
    if not admin_ids:
        logging.warning("ADMIN_IDS is empty; owner commands are disabled")
    await interrupt_all_dialogs_once(bot, get_session_maker())
    await install_command_menu(bot)
    backup_task = asyncio.create_task(backup_loop(bot))
    match_task = asyncio.create_task(match_poll_loop(bot))
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        backup_task.cancel()
        match_task.cancel()
        for task in (backup_task, match_task):
            with contextlib.suppress(asyncio.CancelledError):
                await task
