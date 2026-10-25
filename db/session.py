"""Async engine and session factory (SQLite + aiosqlite)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool

from db.base import Base
from misc.db_url import get_database_url, is_in_memory_sqlite, sqlite_file_path

_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None


def create_async_engine_from_url(url: str | None = None) -> AsyncEngine:
    url = url or get_database_url()
    if is_in_memory_sqlite(url):
        return create_async_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    return create_async_engine(url, poolclass=NullPool)


def get_engine(url: str | None = None) -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine_from_url(url)
    return _engine


def reset_engine() -> None:
    """Test helper: drop cached engine/session maker."""
    global _engine, _session_maker
    _engine = None
    _session_maker = None


def get_session_maker(engine: AsyncEngine | None = None) -> async_sessionmaker[AsyncSession]:
    global _session_maker
    if _session_maker is None:
        eng = engine or get_engine()
        _session_maker = async_sessionmaker(eng, expire_on_commit=False, autoflush=False)
    return _session_maker


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    cfg = Config(str(_project_root() / "alembic.ini"))
    url = get_database_url().replace("%", "%%")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def run_alembic_upgrade_sync() -> None:
    """Apply migrations (file-based DB). Call from thread or sync context."""
    command.upgrade(_alembic_config(), "head")


async def init_db_schema(engine: AsyncEngine | None = None) -> None:
    """
    Ensure schema exists: create_all for in-memory SQLite; Alembic upgrade for file DB.
    """
    import db.models  # noqa: F401  # register tables on Base.metadata

    eng = engine or get_engine()
    url = str(eng.url)
    if is_in_memory_sqlite(url):
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    else:
        db_file = sqlite_file_path(get_database_url())
        if db_file is not None:
            db_file.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(run_alembic_upgrade_sync)


def bind_engine(engine: AsyncEngine) -> None:
    """Point the global session maker at ``engine`` (tests)."""
    global _engine, _session_maker
    _engine = engine
    _session_maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
