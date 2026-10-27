"""Owner commands: list, stats, ban, unban, dialog reset, profile deletion, shared price upload."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from datetime import datetime, timezone
from html import escape
from pathlib import Path

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

from db.repositories import kv, labels, users
from db.schemas.user import UserPage, UserRow, UserStats
from jobs.embed_lock import catalog_embed_lock
from matching.catalog import (
    PRICE_PENDING_KEY,
    PRICE_UPLOAD_KEY,
    find_price_xls,
    load_catalog,
    pending_price_path,
    price_count_dropped_sharply,
    price_list_destination,
    replace_price_file,
)
from matching.errors import PipelineError
from misc.admins import get_admin_ids, parse_id_and_reason
from tgbot.dialogs import interrupt_dialog
from tgbot.ui.callbacks import (
    parse_delete_user_callback,
    parse_match_feedback,
    parse_price_embed_callback,
    parse_price_upload_callback,
    parse_users_page,
)

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
    BotCommand(command="price", description="Загрузить прайс (.xls)"),
]

router = Router(name="admin")

# Telegram getFile limit. Larger documents are rejected before the download.
MAX_PRICE_UPLOAD_BYTES = 20 * 1024 * 1024
# One pending workbook at a time. A newer upload replaces the token.
_price_slot_lock = asyncio.Lock()
# Set while this process has claimed «Посчитать», including the wait for the shared lock.
_embed_gate = asyncio.Lock()
_embed_running = False


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
        "• /delete_user &lt;id&gt; — стереть профиль и закрыть доступ.\n"
        "• /price — загрузить общий прайс (.xls)."
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


def _xls_filename(name: str | None) -> bool:
    if name is None:
        return False
    cleaned = name.strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or "\x00" in cleaned:
        return False
    return Path(cleaned).suffix.lower() == ".xls"


def price_document_problem(file_name: str | None, file_size: object) -> str | None:
    """Russian rejection for a document that must not be downloaded, or None when it may be."""
    if not _xls_filename(file_name):
        return "Нужен файл .xls (Excel 97–2003). Файл .xlsx не подходит."
    if isinstance(file_size, bool) or not isinstance(file_size, int) or file_size < 0:
        return "Не вижу размер файла."
    if file_size > MAX_PRICE_UPLOAD_BYTES:
        return "Файл больше 20 МБ. Такой прайс бот не скачивает."
    return None


def format_price_prompt(
    original_name: str,
    new_count: int,
    previous_count: int | None,
    *,
    unreadable_current: bool = False,
) -> str:
    """Confirmation text. The filename is escaped because the bot parse mode is HTML."""
    lines = [
        f"Заменить общий прайс на <b>{escape(original_name)}</b>?",
        f"Позиций в новом файле: {new_count}.",
    ]
    if previous_count is not None:
        lines.append(f"Сейчас в прайсе: {previous_count}.")
        if price_count_dropped_sharply(previous_count, new_count):
            lines.append("В новом файле сильно меньше позиций.")
    elif unreadable_current:
        lines.append("Текущий прайс есть, но позиции не прочитались.")
    return "\n".join(lines)


def _price_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Заменить", callback_data=f"pp|{token}|ok"),
                InlineKeyboardButton(text="Отмена", callback_data=f"pp|{token}|x"),
            ]
        ]
    )


def _embed_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Посчитать", callback_data="pe|1")]]
    )


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logging.warning("price file remove failed")


def _sku_count_or_reject(path: Path) -> int:
    count = len(load_catalog(str(path)))
    if count < 1:
        raise ValueError("empty price catalog")
    return count


def _live_catalog_count() -> tuple[bool, int | None]:
    """Whether a live file exists, and its SKU count when the workbook parses."""
    try:
        path = find_price_xls()
    except FileNotFoundError:
        return False, None
    try:
        return True, len(load_catalog(path))
    except Exception as exc:
        logging.warning("current price was not counted: %s", type(exc).__name__)
        return True, None


def count_missing_catalog_names() -> int:
    """How many live catalog names are absent from the embedding cache. Does not call the model."""
    from matching.embeddings import Embedder

    names = [sku.name for sku in load_catalog()]
    return len(Embedder().missing(names))


def _scrub_embed_detail(text: str) -> str:
    """Drop the API key and URL query strings. The embeddings layer already redacts the key."""
    from matching import config

    cleaned = text.replace("\r", " ").replace("\n", " ")
    key = (config.OPENAI_API_KEY or "").strip()
    if key:
        cleaned = cleaned.replace(key, "***")
    cleaned = re.sub(r"(https?://[^\s'\"<>]+)\?[^\s'\"<>]*", r"\1", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def embed_failure_notice(exc: BaseException) -> str:
    """Short Russian reason for the owner. The log line is the sanitized PipelineError text."""
    if not isinstance(exc, PipelineError):
        logging.warning("price embed failed: %s", type(exc).__name__)
        return "Не удалось посчитать эмбеддинги."
    detail = _scrub_embed_detail(str(exc))
    logging.warning("price embed failed: %s", detail or type(exc).__name__)
    if "OPENAI_API_KEY is missing" in detail:
        return "Не задан ключ OPENAI_API_KEY. Эмбеддинги не посчитаны."
    prefix = "embedding request failed:"
    if detail.startswith(prefix):
        fragment = detail[len(prefix) :].strip()[:180]
        if fragment:
            return f"Провайдер эмбеддингов отклонил запрос. {escape(fragment)}"
        return "Провайдер эмбеддингов отклонил запрос."
    if detail:
        return f"Не удалось посчитать эмбеддинги. {escape(detail[:180])}"
    return "Не удалось посчитать эмбеддинги."


def embed_live_catalog() -> int:
    """Embed live catalog names that are not cached. Returns how many new texts were stored."""
    from matching.embeddings import Embedder, embed_catalog

    names = [sku.name for sku in load_catalog()]
    embedder = Embedder()
    embed_catalog(embedder, names, allow_large=True)
    return embedder.new_texts


def _pending_record(payload: object, token: str) -> dict | None:
    """The stored upload when ``token`` is current and the record has the expected fields."""
    if not isinstance(payload, dict) or payload.get("token") != token:
        return None
    original = payload.get("original_name")
    new_count = payload.get("new_count")
    user_id = payload.get("telegram_user_id")
    previous = payload.get("previous_count")
    if not isinstance(original, str) or not original:
        return None
    if isinstance(new_count, bool) or not isinstance(new_count, int) or new_count < 1:
        return None
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        return None
    if previous is not None and (isinstance(previous, bool) or not isinstance(previous, int)):
        return None
    return payload


async def _edit_query(
    query: CallbackQuery,
    bot: Bot,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    if query.message is not None:
        try:
            await query.message.edit_text(text, reply_markup=markup)
            return
        except Exception:
            logging.debug("price message edit failed")
    if query.from_user is not None:
        await bot.send_message(query.from_user.id, text, reply_markup=markup)


async def _notify_price_replaced(bot: Bot, uploader_id: int, new_count: int) -> None:
    """Tell the other owners. A failed send does not roll back the file."""
    text = f"Прайс заменён. Позиций: {new_count}. Загрузил id {uploader_id}."
    try:
        admin_ids = sorted(get_admin_ids())
    except Exception as exc:
        logging.warning("price notice skipped: %s", type(exc).__name__)
        return
    for admin_id in admin_ids:
        if admin_id == uploader_id:
            continue
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:
            logging.warning("price notice failed chat=%s (%s)", admin_id, type(exc).__name__)


async def _claim_embed() -> bool:
    """False when an embed is already claimed or the shared cache lock is held."""
    global _embed_running
    async with _embed_gate:
        if _embed_running or catalog_embed_lock.locked():
            return False
        _embed_running = True
        return True


async def _release_embed() -> None:
    global _embed_running
    async with _embed_gate:
        _embed_running = False


async def _replaced_message(new_count: int) -> tuple[str, InlineKeyboardMarkup | None]:
    lines = [f"Прайс заменён. Позиций: {new_count}."]
    markup: InlineKeyboardMarkup | None = None
    try:
        if catalog_embed_lock.locked() or _embed_running:
            lines.append("Сейчас уже идёт расчёт эмбеддингов.")
        else:
            async with catalog_embed_lock:
                missing = await asyncio.to_thread(count_missing_catalog_names)
            if missing > 0:
                lines.append(f"В кэше нет эмбеддингов для {missing} названий.")
                markup = _embed_keyboard()
            else:
                lines.append("Названия уже есть в кэше.")
    except Exception as exc:
        logging.warning("price embed cache check failed: %s", type(exc).__name__)
        lines.append("Не удалось проверить кэш эмбеддингов.")
        markup = _embed_keyboard()
    return "\n".join(lines), markup


@router.message(Command("price"), AdminActor(), F.document)
async def cmd_price_document(message: Message, session: AsyncSession, bot: Bot) -> None:
    """Store one shared workbook and ask before replacing the live file."""
    document = message.document
    if document is None or message.from_user is None:
        return
    problem = price_document_problem(document.file_name, document.file_size)
    if problem is not None:
        await message.answer(problem)
        return
    original_name = (document.file_name or "").strip()
    async with _price_slot_lock:
        pending = pending_price_path()
        part = pending.with_name(pending.name + ".part")
        pending.parent.mkdir(parents=True, exist_ok=True)
        try:
            await bot.download(document, destination=part, timeout=120)
            part.replace(pending)
        except Exception as exc:
            _remove_file(part)
            logging.warning("price download failed: %s", type(exc).__name__)
            await message.answer("Не удалось скачать файл. Текущий прайс не изменён.")
            return
        try:
            new_count = await asyncio.to_thread(_sku_count_or_reject, pending)
        except Exception as exc:
            _remove_file(pending)
            await kv.delete_key(session, PRICE_PENDING_KEY)
            logging.warning("price workbook rejected: %s", type(exc).__name__)
            await message.answer(
                "Не удалось прочитать прайс. Нужен непустой .xls хотя бы с одной позицией. "
                "Текущий прайс не изменён."
            )
            return
        exists, previous = await asyncio.to_thread(_live_catalog_count)
        token = secrets.token_hex(4)
        await kv.set_json(
            session,
            PRICE_PENDING_KEY,
            {
                "token": token,
                "original_name": original_name,
                "telegram_user_id": message.from_user.id,
                "new_count": new_count,
                "previous_count": previous,
            },
        )
        await session.commit()
        await message.answer(
            format_price_prompt(
                original_name,
                new_count,
                previous,
                unreadable_current=exists and previous is None,
            ),
            reply_markup=_price_keyboard(token),
        )


@router.message(Command("price"), AdminActor())
async def cmd_price(message: Message) -> None:
    """Show whether a shared price file is present, then remind how to upload it."""
    exists, previous = await asyncio.to_thread(_live_catalog_count)
    if not exists:
        await message.answer("Прайс ещё не загружен. Пришлите документ .xls и укажите в подписи команду /price.")
        return
    lines: list[str] = []
    if previous is None:
        lines.append("Текущий прайс есть, но позиции не прочитались.")
    else:
        lines.append(f"Текущий общий прайс загружен. Позиций в прайсе: {previous}.")
    lines.append("Чтобы заменить файл, пришлите новый документ .xls и укажите в подписи команду /price.")
    await message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("pp|"), AdminActor())
async def on_price_upload(query: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    try:
        action, token = parse_price_upload_callback(query.data or "")
    except (ValueError, IndexError):
        await query.answer("Некорректная кнопка.", show_alert=True)
        return
    async with _price_slot_lock:
        try:
            payload = await kv.get_json(session, PRICE_PENDING_KEY)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and payload.get("token") == token and _pending_record(payload, token) is None:
            _remove_file(pending_price_path())
            await kv.delete_key(session, PRICE_PENDING_KEY)
            await session.commit()
            await query.answer("Эта загрузка уже неактуальна.", show_alert=True)
            return
        pending = _pending_record(payload, token)
        if pending is None:
            await query.answer("Эта загрузка уже неактуальна.", show_alert=True)
            return
        if action == "x":
            _remove_file(pending_price_path())
            await kv.delete_key(session, PRICE_PENDING_KEY)
            await session.commit()
            await query.answer("Загрузка отменена.")
            await _edit_query(query, bot, "Загрузка отменена. Прежний прайс на месте.")
            return
        workbook = pending_price_path()
        if not workbook.is_file():
            await kv.delete_key(session, PRICE_PENDING_KEY)
            await session.commit()
            await query.answer("Эта загрузка уже неактуальна.", show_alert=True)
            return
        await query.answer()
        destination = price_list_destination()
        try:
            await asyncio.to_thread(replace_price_file, workbook, destination)
        except OSError:
            logging.warning("price replace failed")
            await _edit_query(query, bot, "Не удалось заменить прайс. Текущий файл не изменён.")
            return
        await kv.set_json(
            session,
            PRICE_UPLOAD_KEY,
            {
                "original_name": pending["original_name"],
                "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "telegram_user_id": pending["telegram_user_id"],
                "path": str(destination),
            },
        )
        await kv.delete_key(session, PRICE_PENDING_KEY)
        await session.commit()
        new_count = int(pending["new_count"])
        uploader_id = int(pending["telegram_user_id"])
    text, markup = await _replaced_message(new_count)
    await _edit_query(query, bot, text, markup)
    await _notify_price_replaced(bot, uploader_id, new_count)


@router.callback_query(F.data.startswith("pe|"), AdminActor())
async def on_price_embed(query: CallbackQuery, bot: Bot) -> None:
    try:
        parse_price_embed_callback(query.data or "")
    except (ValueError, IndexError):
        await query.answer("Некорректная кнопка.", show_alert=True)
        return
    if not await _claim_embed():
        await query.answer("Расчёт уже выполняется.", show_alert=True)
        return
    try:
        await query.answer()
        await _edit_query(query, bot, "Считаю эмбеддинги.")
        async with catalog_embed_lock:
            added = await asyncio.to_thread(embed_live_catalog)
    except Exception as exc:
        await _edit_query(query, bot, embed_failure_notice(exc))
        return
    finally:
        await _release_embed()
    await _edit_query(query, bot, f"Посчитано новых названий: {added}.")
