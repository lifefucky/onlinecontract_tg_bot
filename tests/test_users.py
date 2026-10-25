"""Users, owner commands, and the one-time dialog reset."""

import os

import pytest

from tgbot.admin import format_stats, format_user_card, format_user_list
from tgbot.middleware import should_deny_access
from db.enums import StartResult
from db.repositories import chats, kv, message_tracks, users
from db.repositories.kv import DIALOG_RESET_ALL_KEY
from db.schemas.chat import ChatUpsert
from db.schemas.user import UserPage, UserRow, UserStart, UserStats
from db.session import bind_engine, create_async_engine_from_url, get_session_maker, init_db_schema, reset_engine
from tgbot.dialogs import interrupt_all_dialogs_once
from misc.admins import get_admin_ids, parse_id_and_reason


@pytest.fixture(autouse=True)
def _isolate_database_url():
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
    yield
    if prev is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = prev
    reset_engine()


@pytest.fixture
async def session():
    reset_engine()
    engine = create_async_engine_from_url(os.environ["DATABASE_URL"])
    bind_engine(engine)
    await init_db_schema(engine)
    maker = get_session_maker()
    async with maker() as db_session:
        yield db_session
        await db_session.commit()


def _start(user_id: int, **kwargs: str) -> UserStart:
    return UserStart(
        telegram_user_id=user_id,
        username=kwargs.get("username"),
        first_name=kwargs.get("first_name", "Анна"),
        last_name=kwargs.get("last_name"),
        language_code=kwargs.get("language_code", "ru"),
    )


@pytest.mark.asyncio
async def test_register_start_updates_profile_and_keeps_started_at(session):
    created, outcome = await users.register_start(session, _start(7, username="old"))
    assert outcome is StartResult.created
    assert created.started_at is not None

    updated, outcome = await users.register_start(session, _start(7, username="new", first_name="Борис"))
    assert outcome is StartResult.updated
    assert updated.id == created.id
    assert updated.username == "new"
    assert updated.first_name == "Борис"
    assert updated.started_at == created.started_at


@pytest.mark.asyncio
async def test_delete_profile_keeps_start_closed(session):
    await users.register_start(session, _start(8, username="keep", first_name="Ира"))
    deleted = await users.delete_user_profile(session, 8)
    assert deleted.is_banned is True
    assert deleted.profile_cleared is True
    assert deleted.username is None
    assert deleted.first_name is None

    again, outcome = await users.register_start(session, _start(8, username="back", first_name="Ира"))
    assert outcome is StartResult.banned
    assert again.first_name is None
    assert again.username is None
    assert await users.note_seen_and_is_banned(session, 8) is True


@pytest.mark.asyncio
async def test_unban_allows_profile_to_return(session):
    await users.register_start(session, _start(9, first_name="Олег"))
    await users.delete_user_profile(session, 9)
    opened = await users.unban_user(session, 9)
    assert opened is not None
    assert opened.is_banned is False

    restored, outcome = await users.register_start(session, _start(9, first_name="Олег", username="oleg"))
    assert outcome is StartResult.updated
    assert restored.first_name == "Олег"
    assert restored.username == "oleg"
    assert restored.profile_cleared is False
    assert await users.note_seen_and_is_banned(session, 9) is False


@pytest.mark.asyncio
async def test_ban_unknown_id_is_not_counted_as_a_start(session):
    tombstone = await users.ban_user(session, 11, "спам")
    assert tombstone.started_at is None
    assert tombstone.is_banned is True
    assert tombstone.ban_reason == "спам"

    stats = await users.user_stats(session)
    assert stats.total_started == 0
    assert stats.banned == 1

    blocked, outcome = await users.register_start(session, _start(11, first_name="Нет"))
    assert outcome is StartResult.banned
    assert blocked.first_name is None


@pytest.mark.asyncio
async def test_stats_and_list_for_a_started_user(session):
    await users.register_start(session, _start(12, username="anna", first_name="<Анна>"))
    await users.mark_bot_blocked(session, 12, blocked=True)
    stats = await users.user_stats(session)
    assert stats == UserStats(total_started=1, started_today=1, seen_7d=1, banned=0, blocked_bot=1)

    page = await users.list_users(session, offset=0, limit=8)
    assert page.total == 1
    text = format_user_list(page)
    assert "&lt;Анна&gt;" in text
    card = format_user_card(page.items[0])
    assert "заблокировал бота" in card
    assert format_stats(stats).startswith("<b>Сводка</b>")


@pytest.mark.asyncio
async def test_mark_bot_blocked_ignores_unknown_user(session):
    await users.mark_bot_blocked(session, 404, blocked=True)
    assert await users.get_user(session, 404) is None


@pytest.mark.asyncio
async def test_startup_dialog_reset_runs_once(session):
    chat = await chats.upsert_chat(session, ChatUpsert(telegram_chat_id=100, chat_type="private", title=None))
    await message_tracks.add_track(
        session,
        chat_internal_id=chat.id,
        telegram_message_id=5,
        scope="welcome",
        sort_order=0,
    )
    await session.commit()

    class FakeBot:
        def __init__(self) -> None:
            self.deleted: list[tuple[int, int]] = []
            self.unpinned: list[int] = []

        async def delete_message(self, chat_id: int, message_id: int) -> None:
            self.deleted.append((chat_id, message_id))

        async def unpin_all_chat_messages(self, chat_id: int) -> None:
            self.unpinned.append(chat_id)

    fake = FakeBot()
    maker = get_session_maker()
    await interrupt_all_dialogs_once(fake, maker)
    await interrupt_all_dialogs_once(fake, maker)

    assert fake.deleted == [(100, 5)]
    assert fake.unpinned == [100]
    async with maker() as fresh:
        assert await kv.get_bool(fresh, DIALOG_RESET_ALL_KEY) is True
        snapshot = await message_tracks.snapshot_dialogs(fresh)
        assert snapshot == {100: []}


def test_is_in_memory_detects_encoded_sqlalchemy_url():
    from misc.db_url import is_in_memory_sqlite

    assert is_in_memory_sqlite("sqlite+aiosqlite:///:memory:") is True
    assert is_in_memory_sqlite("sqlite+aiosqlite:///%3Amemory%3A") is True
    assert is_in_memory_sqlite("sqlite+aiosqlite:///./data/app.db") is False


def test_should_deny_access_keeps_owner():
    assert should_deny_access(is_banned=True, is_admin=False) is True
    assert should_deny_access(is_banned=True, is_admin=True) is False
    assert should_deny_access(is_banned=False, is_admin=False) is False


def test_admin_ids_and_command_args(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_IDS", "10, 20,,")
    assert get_admin_ids() == frozenset({10, 20})
    monkeypatch.setenv("ADMIN_IDS", "nope")
    with pytest.raises(RuntimeError):
        get_admin_ids()
    assert parse_id_and_reason("15 причина текста") == (15, "причина текста")
    assert parse_id_and_reason("  ") is None
    assert parse_id_and_reason("user") is None


def test_format_user_list_empty():
    assert format_user_list(UserPage(items=[], total=0, offset=0)) == "Пока никто не записан."


def test_user_row_roundtrip_for_card():
    from datetime import datetime, timezone

    row = UserRow(
        id=1,
        telegram_user_id=5,
        username=None,
        first_name=None,
        last_name=None,
        language_code=None,
        is_banned=True,
        bot_blocked=False,
        profile_cleared=True,
        started_at=datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc),
        last_seen_at=datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc),
        banned_at=None,
        ban_reason="удаление профиля",
    )
    card = format_user_card(row)
    assert "профиль удалён" in card
    assert "удалён, доступ закрыт" in card
