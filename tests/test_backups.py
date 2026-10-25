"""SQLite snapshot path, backup interval, and skip rules."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import db.models  # noqa: F401  # register tables on Base.metadata
from backups import (
    BACKUP_STATE_KEY,
    backup_delay_until_attempt,
    backup_interval_seconds,
    backup_on_start,
    load_backup_state,
    plan_backup,
    send_db_backup,
    snapshot_content_hash,
    snapshot_sqlite,
)
from db.base import Base
from db.repositories import kv, users
from db.schemas.user import UserStart
from db.session import bind_engine, create_async_engine_from_url, get_session_maker, reset_engine
from misc.db_url import sqlite_file_path


def test_sqlite_file_path_memory_and_relative():
    assert sqlite_file_path("sqlite+aiosqlite:///:memory:") is None
    relative = sqlite_file_path("sqlite+aiosqlite:///./data/app.db")
    assert relative is not None
    assert relative.name == "app.db"
    absolute = sqlite_file_path("sqlite+aiosqlite:////tmp/oc-app.db")
    assert absolute is not None
    assert absolute.is_absolute()
    assert absolute.name == "oc-app.db"


def test_snapshot_sqlite_copies_rows(tmp_path):
    source = tmp_path / "app.db"
    connection = sqlite3.connect(source)
    connection.execute("create table t (id integer)")
    connection.execute("insert into t values (1)")
    connection.commit()
    connection.close()

    dest = tmp_path / "snap.db"
    snapshot_sqlite(source, dest)
    copied = sqlite3.connect(dest)
    try:
        assert copied.execute("select id from t").fetchone()[0] == 1
    finally:
        copied.close()


def test_backup_interval_hours(monkeypatch):
    monkeypatch.setenv("DB_BACKUP_INTERVAL_HOURS", "6")
    assert backup_interval_seconds() == 6 * 3600
    monkeypatch.setenv("DB_BACKUP_INTERVAL_HOURS", "0")
    assert backup_interval_seconds() == 24 * 3600
    monkeypatch.setenv("DB_BACKUP_INTERVAL_HOURS", "no")
    assert backup_interval_seconds() == 24 * 3600


def test_backup_on_start_flag(monkeypatch):
    monkeypatch.setenv("DB_BACKUP_ON_START", "1")
    assert backup_on_start() is True
    monkeypatch.setenv("DB_BACKUP_ON_START", "yes")
    assert backup_on_start() is True
    monkeypatch.setenv("DB_BACKUP_ON_START", "0")
    assert backup_on_start() is False
    monkeypatch.delenv("DB_BACKUP_ON_START", raising=False)
    assert backup_on_start() is False
    monkeypatch.setenv("DB_BACKUP_ON_START", "maybe")
    assert backup_on_start() is False


def test_plan_backup_waits_for_the_rest_of_the_interval():
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    last = now - timedelta(hours=1)
    plan = plan_backup(last, now=now, interval=24 * 3600, on_start=False, first_tick=True)
    assert plan.delay_seconds == 23 * 3600
    assert plan.anchor_attempt is False


def test_plan_backup_defers_a_due_send_when_start_send_is_off():
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    last = now - timedelta(hours=25)
    plan = plan_backup(last, now=now, interval=24 * 3600, on_start=False, first_tick=True)
    assert plan.delay_seconds == 24 * 3600
    assert plan.anchor_attempt is True


def test_plan_backup_sends_a_due_backup_on_start_when_enabled():
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    plan = plan_backup(None, now=now, interval=24 * 3600, on_start=True, first_tick=True)
    assert plan.delay_seconds == 0
    assert plan.anchor_attempt is False


def test_plan_backup_later_tick_sends_when_due():
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    last = now - timedelta(hours=25)
    plan = plan_backup(last, now=now, interval=24 * 3600, on_start=False, first_tick=False)
    assert plan.delay_seconds == 0
    assert plan.anchor_attempt is False


def test_plan_backup_clock_step_back_waits_full_interval():
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    last = now + timedelta(minutes=5)
    plan = plan_backup(last, now=now, interval=3600, on_start=True, first_tick=True)
    assert plan.delay_seconds == 3600
    assert plan.anchor_attempt is False


def test_snapshot_content_hash_ignores_backup_state(tmp_path):
    path = tmp_path / "app.db"
    connection = sqlite3.connect(path)
    connection.execute("create table notes (id integer, body text, updated_at text)")
    connection.execute("create table app_kv_store (key text, value_json text)")
    connection.execute("insert into notes values (1, 'a', 't0')")
    connection.commit()
    first = snapshot_content_hash(path)
    connection.execute(
        "insert into app_kv_store values (?, ?)",
        (BACKUP_STATE_KEY, '{"attempted_at":"2026-10-24T00:00:00+00:00"}'),
    )
    connection.execute("update notes set updated_at = 't1' where id = 1")
    connection.commit()
    assert snapshot_content_hash(path) == first
    connection.execute("update notes set body = 'b' where id = 1")
    connection.commit()
    assert snapshot_content_hash(path) != first
    connection.close()


class _Bot:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[int] = []

    async def send_document(self, chat_id, document, caption=None):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(chat_id)


@pytest.fixture
async def file_db(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    url = "sqlite+aiosqlite:///" + path.resolve().as_posix()
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("ADMIN_IDS", "42")
    monkeypatch.setenv("DB_BACKUP_ON_START", "0")
    monkeypatch.setenv("DB_BACKUP_INTERVAL_HOURS", "24")
    reset_engine()
    engine = create_async_engine_from_url(url)
    bind_engine(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield path
    await engine.dispose()
    reset_engine()


async def test_send_skips_unchanged_database_and_sends_after_a_write(file_db):
    bot = _Bot()
    assert await send_db_backup(bot) == "sent"
    assert bot.sent == [42]
    assert await send_db_backup(bot) == "unchanged"
    assert bot.sent == [42]

    maker = get_session_maker()
    async with maker() as session:
        await users.register_start(
            session,
            UserStart(telegram_user_id=7, first_name="Анна", language_code="ru"),
        )
        await session.commit()

    assert await send_db_backup(bot) == "sent"
    assert bot.sent == [42, 42]


async def test_failed_send_does_not_record_the_snapshot(file_db):
    assert await send_db_backup(_Bot(fail=True)) == "failed"
    last_attempt, content_hash = await load_backup_state()
    assert last_attempt is None
    assert content_hash is None

    bot = _Bot()
    assert await send_db_backup(bot) == "sent"
    assert bot.sent == [42]


async def test_startup_deferral_keeps_the_previous_hash(file_db):
    bot = _Bot()
    assert await send_db_backup(bot) == "sent"
    _attempt, digest = await load_backup_state()
    assert digest

    maker = get_session_maker()
    async with maker() as session:
        await kv.set_json(
            session,
            BACKUP_STATE_KEY,
            {
                "attempted_at": (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(),
                "content_hash": digest,
            },
        )
        await session.commit()

    delay = await backup_delay_until_attempt(first_tick=True)
    assert delay == 24 * 3600
    kept_attempt, kept_hash = await load_backup_state()
    assert kept_hash == digest
    assert kept_attempt is not None
    assert datetime.now(timezone.utc) - kept_attempt < timedelta(minutes=1)

    assert await send_db_backup(bot) == "unchanged"
    assert bot.sent == [42]
