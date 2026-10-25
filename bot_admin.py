"""Owner commands: list, stats, ban, unban, dialog reset, profile deletion."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from db.repositories import labels, users
from db.schemas.user import UserPage, UserRow, UserStats
from dialogs import interrupt_dialog
from misc.admins import get_admin_ids, parse_id_and_reason
from utils.callbacks import parse_delete_user_callback, parse_match_feedback, parse_users_page

USERS_PAGE_SIZE = 8

USER_COMMANDS = [
    BotCommand(command="start", description="Выбрать категорию процедур"),
    BotCommand(command="help", description="Как пользоваться ботом"),
    BotCommand(command="clear", description="Удалить последние сообщения"),
]
ADMIN_COMMANDS = [
    BotCommand(command="stats", description="Сводка по пользователям"),
    BotCommand(command="users", description="Список пользователей"),
    BotCommand(command="user", description="Карточка по id"),
    BotCommand(command="ban", description="Закрыть доступ"),
    BotCommand(command="unban", description="Снова открыть доступ"),
    BotCommand(command="reset", description="Сбросить диалог"),
    BotCommand(command="delete_user", description="Удалить профиль и закрыть доступ"),
]

router = Router(name="admin")


class AdminActor(BaseFilter):
    """Owner commands only in a private chat, checked by numeric id rather than the menu scope."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        if user is None or user.id not in get_admin_ids():
            return False
        chat = event.chat if isinstance(event, Message) else getattr(event.message, "chat", None)
        if chat is None:
            return False
        raw = chat.type.value if hasattr(chat.type, "value") else str(chat.type)
        return raw == "private"


def admin_help_text() -> str:
    return (
        "\n\nКоманды владельца:\n"
        "• /stats — сводка.\n"
        "• /users — список.\n"
        "• /user &lt;id&gt; — карточка.\n"
        "• /ban &lt;id&gt; [причина] — закрыть доступ.\n"
        "• /unban &lt;id&gt; — открыть доступ.\n"
        "• /reset &lt;id&gt; — сбросить диалог, доступ оставить.\n"
        "• /delete_user &lt;id&gt; — стереть профиль и закрыть доступ."
    )


def display_name(user: UserRow) -> str:
    if user.profile_cleared:
        return "профиль удалён"
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(part for part in parts if part).strip()
    return name or "без имени"


def status_label(user: UserRow) -> str:
    if user.is_banned and user.profile_cleared:
        return "удалён, доступ закрыт"
    if user.is_banned:
        return "доступ закрыт"
    if user.bot_blocked:
        return "заблокировал бота"
    return "активен"


def format_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%d %H:%M UTC")


def format_stats(stats: UserStats) -> str:
    return (
        "<b>Сводка</b>\n"
        f"Всего запускали бота: {stats.total_started}\n"
        f"Стартовали сегодня: {stats.started_today}\n"
        f"Заходили за 7 дней: {stats.seen_7d}\n"
        f"Доступ закрыт: {stats.banned}\n"
        f"Сами заблокировали бота: {stats.blocked_bot}"
    )


def format_user_card(user: UserRow) -> str:
    username = f"@{escape(user.username)}" if user.username else "—"
    language = escape(user.language_code) if user.language_code else "—"
    reason = escape(user.ban_reason) if user.ban_reason else "—"
    return (
        f"<b>id</b> <code>{user.telegram_user_id}</code>\n"
        f"<b>Имя</b> {escape(display_name(user))}\n"
        f"<b>Username</b> {username}\n"
        f"<b>Язык</b> {language}\n"
        f"<b>Статус</b> {status_label(user)}\n"
        f"<b>Старт</b> {format_dt(user.started_at)}\n"
        f"<b>Последний раз</b> {format_dt(user.last_seen_at)}\n"
        f"<b>Причина запрета</b> {reason}"
    )


def format_user_list(page: UserPage) -> str:
    if page.total == 0:
        return "Пока никто не записан."
    if not page.items:
        return "На этой странице никого нет. Откройте /users."
    start = page.offset + 1
    end = page.offset + len(page.items)
    lines = [f"Пользователи {start}–{end} из {page.total}"]
    for user in page.items:
        username = f"@{escape(user.username)}" if user.username else "—"
        lines.append(
            f"<code>{user.telegram_user_id}</code> {escape(display_name(user))} {username}"
            f" · {status_label(user)} · {format_dt(user.started_at)}"
        )
    return "\n".join(lines)


def users_keyboard(page: UserPage) -> InlineKeyboardMarkup | None:
    nav: list[InlineKeyboardButton] = []
    if page.offset > 0:
        prev_offset = max(0, page.offset - USERS_PAGE_SIZE)
        nav.append(InlineKeyboardButton(text="Назад", callback_data=f"au|{prev_offset}"))
    if page.offset + USERS_PAGE_SIZE < page.total:
        nav.append(
            InlineKeyboardButton(
                text="Дальше",
                callback_data=f"au|{page.offset + USERS_PAGE_SIZE}",
            )
        )
    if not nav:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[nav])


def _protected(user_id: int) -> bool:
    return user_id in get_admin_ids()


def _actor_id(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None


async def install_command_menu(bot: Bot) -> None:
    """Default menu for everyone; owner commands only in each admin's private chat."""
    await bot.set_my_commands(USER_COMMANDS)
    for admin_id in sorted(get_admin_ids()):
        try:
            await bot.set_my_commands(
                [*USER_COMMANDS, *ADMIN_COMMANDS],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception:
            logging.warning("set_my_commands failed for admin %s", admin_id, exc_info=True)


def register_admin_handlers(dispatcher: Dispatcher) -> None:
    dispatcher.include_router(router)


@router.message(Command("stats"), AdminActor())
async def cmd_stats(message: Message, session: AsyncSession) -> None:
    stats = await users.user_stats(session)
    await message.answer(format_stats(stats))


@router.message(Command("users"), AdminActor())
async def cmd_users(message: Message, session: AsyncSession) -> None:
    page = await users.list_users(session, offset=0, limit=USERS_PAGE_SIZE)
    await message.answer(format_user_list(page), reply_markup=users_keyboard(page))


@router.message(Command("user"), AdminActor())
async def cmd_user(message: Message, command: CommandObject, session: AsyncSession) -> None:
    parsed = parse_id_and_reason(command.args)
    if parsed is None:
        await message.answer("Укажите id: /user 123456789")
        return
    user_id, _reason = parsed
    row = await users.get_user(session, user_id)
    if row is None:
        await message.answer("Пользователь не найден.")
        return
    await message.answer(format_user_card(row))


@router.message(Command("ban"), AdminActor())
async def cmd_ban(message: Message, command: CommandObject, session: AsyncSession, bot: Bot) -> None:
    parsed = parse_id_and_reason(command.args)
    if parsed is None:
        await message.answer("Укажите id: /ban 123456789")
        return
    user_id, reason = parsed
    if _protected(user_id):
        await message.answer("Этот id указан в ADMIN_IDS, команда не выполнена.")
        return
    await users.ban_user(session, user_id, reason)
    await interrupt_dialog(bot, session, user_id)
    logging.info("user banned telegram_user_id=%s by=%s", user_id, _actor_id(message))
    await message.answer(f"Доступ закрыт для id <code>{user_id}</code>.")


@router.message(Command("unban"), AdminActor())
async def cmd_unban(message: Message, command: CommandObject, session: AsyncSession) -> None:
    parsed = parse_id_and_reason(command.args)
    if parsed is None:
        await message.answer("Укажите id: /unban 123456789")
        return
    user_id, _reason = parsed
    row = await users.unban_user(session, user_id)
    if row is None:
        await message.answer("Пользователь не найден.")
        return
    logging.info("user unbanned telegram_user_id=%s by=%s", user_id, _actor_id(message))
    await message.answer(f"Доступ снова открыт для id <code>{user_id}</code>.")


@router.message(Command("reset"), AdminActor())
async def cmd_reset(message: Message, command: CommandObject, session: AsyncSession, bot: Bot) -> None:
    parsed = parse_id_and_reason(command.args)
    if parsed is None:
        await message.answer("Укажите id: /reset 123456789")
        return
    user_id, _reason = parsed
    await interrupt_dialog(bot, session, user_id)
    logging.info("dialog reset telegram_user_id=%s by=%s", user_id, _actor_id(message))
    await message.answer(f"Диалог сброшен для id <code>{user_id}</code>.")


@router.message(Command("delete_user"), AdminActor())
async def cmd_delete_user(message: Message, command: CommandObject) -> None:
    parsed = parse_id_and_reason(command.args)
    if parsed is None:
        await message.answer("Укажите id: /delete_user 123456789")
        return
    user_id, _reason = parsed
    if _protected(user_id):
        await message.answer("Этот id указан в ADMIN_IDS, команда не выполнена.")
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Удалить", callback_data=f"adel|{user_id}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"adelx|{user_id}"),
            ]
        ]
    )
    await message.answer(
        f"Удалить профиль id <code>{user_id}</code>? Повторный /start будет закрыт.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("mf|"), AdminActor())
async def on_match_feedback(query: CallbackQuery, session: AsyncSession) -> None:
    try:
        procedure_id, decision, token = parse_match_feedback(query.data or "")
    except (ValueError, IndexError):
        await query.answer("Некорректная кнопка.", show_alert=True)
        return
    if query.from_user is None:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return
    saved = await labels.record_feedback(
        session,
        procedure_id=procedure_id,
        content_token=token,
        decision=decision,
        telegram_user_id=query.from_user.id,
    )
    if not saved:
        await query.answer("Нет данных этого листа. Ответ не сохранён.", show_alert=True)
        return
    text = "Ответ сохранён: подходит." if decision == "fit" else "Ответ сохранён: не подходит."
    await query.answer(text)
    logging.info(
        "match label procedure=%s decision=%s by=%s",
        procedure_id,
        decision,
        query.from_user.id,
    )


@router.callback_query(F.data.startswith("au|"), AdminActor())
async def on_users_page(query: CallbackQuery, session: AsyncSession) -> None:
    try:
        offset = parse_users_page(query.data or "")
    except (ValueError, IndexError):
        await query.answer("Некорректная кнопка.", show_alert=True)
        return
    await query.answer()
    page = await users.list_users(session, offset=offset, limit=USERS_PAGE_SIZE)
    text = format_user_list(page)
    markup = users_keyboard(page)
    if query.message is not None:
        try:
            await query.message.edit_text(text, reply_markup=markup)
            return
        except Exception:
            logging.debug("users page edit failed", exc_info=True)
    if query.from_user is not None:
        await query.bot.send_message(query.from_user.id, text, reply_markup=markup)


@router.callback_query(F.data.startswith("adel"), AdminActor())
async def on_delete_user(query: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    try:
        action, user_id = parse_delete_user_callback(query.data or "")
    except (ValueError, IndexError):
        await query.answer("Некорректная кнопка.", show_alert=True)
        return
    if action == "adelx":
        await query.answer("Удаление отменено.")
        if query.message is not None:
            try:
                await query.message.edit_text("Удаление отменено.")
            except Exception:
                logging.debug("delete cancel edit failed", exc_info=True)
        return
    if _protected(user_id):
        await query.answer("Этот id указан в ADMIN_IDS.", show_alert=True)
        return
    await query.answer()
    await users.delete_user_profile(session, user_id)
    await interrupt_dialog(bot, session, user_id)
    logging.info(
        "user deleted telegram_user_id=%s by=%s",
        user_id,
        query.from_user.id if query.from_user else None,
    )
    text = f"Профиль удалён, доступ закрыт для id <code>{user_id}</code>."
    if query.message is not None:
        try:
            await query.message.edit_text(text)
            return
        except Exception:
            logging.debug("delete confirm edit failed", exc_info=True)
    if query.from_user is not None:
        await query.bot.send_message(query.from_user.id, text)
