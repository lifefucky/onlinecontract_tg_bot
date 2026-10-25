"""Send admins a SQLite snapshot on a saved interval, and only when the data changed."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from os import environ
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile

from db.repositories import kv
from db.session import get_session_maker
from misc.admins import get_admin_ids
from misc.db_url import get_database_url, is_in_memory_sqlite, sqlite_file_path
from misc.load_env import load_project_env

DEFAULT_BACKUP_INTERVAL_HOURS = 24.0
BACKUP_STATE_KEY = "db_backup_state"

_ON_VALUES = {"1", "true", "yes", "on"}
_OFF_VALUES = {"0", "false", "no", "off", ""}


@dataclass(frozen=True)
class BackupPlan:
    """How long to wait, and whether this start should pin the next attempt without sending."""

    delay_seconds: float
    anchor_attempt: bool


def backup_interval_seconds() -> float:
    load_project_env()
    raw = environ.get("DB_BACKUP_INTERVAL_HOURS", "").strip()
    if not raw:
        return DEFAULT_BACKUP_INTERVAL_HOURS * 3600
    try:
        hours = float(raw.replace(",", "."))
    except ValueError:
        logging.warning("DB_BACKUP_INTERVAL_HOURS=%r is not a number; using 24", raw)
        return DEFAULT_BACKUP_INTERVAL_HOURS * 3600
    if hours <= 0:
        logging.warning("DB_BACKUP_INTERVAL_HOURS=%s is not positive; using 24", hours)
        return DEFAULT_BACKUP_INTERVAL_HOURS * 3600
    return hours * 3600


def backup_on_start() -> bool:
    """True when a due backup may be sent during process startup."""
    load_project_env()
    raw = environ.get("DB_BACKUP_ON_START", "0").strip().lower()
    if raw in _ON_VALUES:
        return True
    if raw in _OFF_VALUES:
        return False
    logging.warning("DB_BACKUP_ON_START=%r is not a flag; not sending on start", raw)
    return False


def plan_backup(
    last_attempt: datetime | None,
    *,
    now: datetime,
    interval: float,
    on_start: bool,
    first_tick: bool,
) -> BackupPlan:
    """Delay until the next attempt. A due backup is postponed one interval when start-send is off."""
    if last_attempt is None:
        remaining = 0.0
    else:
        elapsed = (now - last_attempt).total_seconds()
        if elapsed < 0:
            elapsed = 0.0
        remaining = interval - elapsed
        if remaining < 0:
            remaining = 0.0
    if first_tick and not on_start and remaining == 0.0:
        return BackupPlan(interval, True)
    return BackupPlan(remaining, False)


def _parse_attempt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _state_from_payload(payload: object) -> tuple[datetime | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    digest = payload.get("content_hash")
    content_hash = digest if isinstance(digest, str) and digest else None
    return _parse_attempt(payload.get("attempted_at")), content_hash


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _mix_cell(digest, cell: object) -> None:
    if cell is None:
        raw = b""
        kind = b"\x00"
    elif isinstance(cell, bytes):
        raw = cell
        kind = b"\x01"
    else:
        raw = str(cell).encode()
        kind = b"\x02"
    digest.update(kind)
    digest.update(len(raw).to_bytes(8, "big"))
    digest.update(raw)
    digest.update(b"\x1e")


def snapshot_content_hash(path: Path) -> str:
    """Hash table rows. ``db_backup_state`` and ``updated_at`` are left out so bookkeeping is not a change."""
    digest = hashlib.sha256()
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for name in names:
            quoted = _quote_ident(name)
            digest.update(name.encode())
            digest.update(b"\0")
            if name == "app_kv_store":
                cursor = connection.execute(
                    f"SELECT * FROM {quoted} WHERE key != ? ORDER BY rowid",
                    (BACKUP_STATE_KEY,),
                )
            else:
                cursor = connection.execute(f"SELECT * FROM {quoted} ORDER BY rowid")
            columns = [item[0] for item in cursor.description]
            digest.update("\0".join(columns).encode())
            digest.update(b"\0")
            for row in cursor:
                for column, cell in zip(columns, row):
                    # Match polls rewrite the same JSON and bump this clock. That is not a new backup.
                    if column == "updated_at":
                        continue
                    _mix_cell(digest, cell)
                digest.update(b"\x1f")
    finally:
        connection.close()
    return digest.hexdigest()


def snapshot_sqlite(source: Path, dest: Path) -> None:
    """Copy a live SQLite file, including pages written by the open bot connection."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
    src = sqlite3.connect(source_uri, uri=True, timeout=30)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


async def load_backup_state() -> tuple[datetime | None, str | None]:
    maker = get_session_maker()
    async with maker() as session:
        try:
            payload = await kv.get_json(session, BACKUP_STATE_KEY)
        except json.JSONDecodeError:
            logging.warning("db backup state is not valid json; ignoring it")
            return None, None
    return _state_from_payload(payload)


async def save_backup_state(attempted_at: datetime, content_hash: str | None) -> None:
    payload: dict[str, str] = {
        "attempted_at": attempted_at.astimezone(timezone.utc).isoformat(),
    }
    if content_hash:
        payload["content_hash"] = content_hash
    maker = get_session_maker()
    async with maker() as session:
        await kv.set_json(session, BACKUP_STATE_KEY, payload)
        await session.commit()


async def backup_delay_until_attempt(*, first_tick: bool) -> float:
    """Seconds to wait. A due attempt at startup is pinned into the future when start-send is off."""
    interval = backup_interval_seconds()
    now = datetime.now(timezone.utc)
    last_attempt, content_hash = await load_backup_state()
    plan = plan_backup(
        last_attempt,
        now=now,
        interval=interval,
        on_start=backup_on_start(),
        first_tick=first_tick,
    )
    if plan.anchor_attempt:
        await save_backup_state(now, content_hash)
    return plan.delay_seconds


async def send_db_backup(bot: Bot) -> str:
    """Snapshot the file database and send it when its contents changed.

    Returns ``sent``, ``unchanged``, ``skipped``, or ``failed``.
    """
    url = get_database_url()
    if is_in_memory_sqlite(url):
        logging.info("db backup skipped: database is in-memory")
        return "skipped"
    path = sqlite_file_path(url)
    if path is None or not path.is_file():
        logging.warning("db backup skipped: sqlite file is missing")
        return "skipped"
    admin_ids = sorted(get_admin_ids())
    if not admin_ids:
        logging.info("db backup skipped: ADMIN_IDS is empty")
        return "skipped"

    moment = datetime.now(timezone.utc)
    stamp = moment.strftime("%Y%m%d-%H%M")
    hours = backup_interval_seconds() / 3600
    caption = f"Копия базы, {moment.strftime('%Y-%m-%d %H:%M UTC')}. Следующая проверка через {hours:g} ч."
    with tempfile.TemporaryDirectory(prefix="oc-db-") as tmp:
        snapshot = Path(tmp) / f"onlinecontract-{stamp}.db"
        await asyncio.to_thread(snapshot_sqlite, path, snapshot)
        digest = await asyncio.to_thread(snapshot_content_hash, snapshot)
        _last_attempt, previous_hash = await load_backup_state()
        if previous_hash == digest:
            logging.info("db backup skipped: database unchanged")
            await save_backup_state(moment, digest)
            return "unchanged"
        sent = 0
        for admin_id in admin_ids:
            try:
                await bot.send_document(
                    admin_id,
                    FSInputFile(snapshot, filename=snapshot.name),
                    caption=caption,
                )
            except Exception:
                logging.exception("db backup send failed admin=%s", admin_id)
            else:
                sent += 1
    logging.info("db backup sent to %s of %s admins", sent, len(admin_ids))
    if sent == 0:
        return "failed"
    await save_backup_state(moment, digest)
    return "sent"


def _log_backup_wait(delay: float) -> None:
    if delay >= 3600:
        logging.info("db backup next attempt in %.2f h", delay / 3600)
    else:
        logging.info("db backup next attempt in %.0f s", delay)


async def backup_loop(bot: Bot) -> None:
    """Send when the saved interval has elapsed and the database changed, until cancelled."""
    interval = backup_interval_seconds()
    logging.info("db backup every %s h; send on start=%s", interval / 3600, backup_on_start())
    first_tick = True
    while True:
        try:
            delay = await backup_delay_until_attempt(first_tick=first_tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("db backup schedule failed")
            delay = interval
        else:
            first_tick = False
        if delay > 0:
            _log_backup_wait(delay)
            await asyncio.sleep(delay)
            continue
        try:
            outcome = await send_db_backup(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("db backup failed")
            outcome = "failed"
        if outcome not in {"sent", "unchanged"}:
            await asyncio.sleep(interval)
