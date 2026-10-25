"""Database URL from environment (see example.env)."""

from os import environ
from pathlib import Path
from typing import Final
from urllib.parse import unquote

from sqlalchemy.engine.url import make_url

from misc.load_env import load_project_env

# In-memory async SQLite by default; use StaticPool in session factory for a single shared DB.
DEFAULT_DATABASE_URL: Final = "sqlite+aiosqlite:///:memory:"


def get_database_url() -> str:
    load_project_env()
    url = environ.get("DATABASE_URL", DEFAULT_DATABASE_URL).strip()
    return url or DEFAULT_DATABASE_URL


def is_in_memory_sqlite(url: str) -> bool:
    # SQLAlchemy renders sqlite's ``:memory:`` as ``%3Amemory%3A`` in ``str(engine.url)``.
    u = unquote(url).lower()
    return ":memory:" in u or "mode=memory" in u


def sqlite_file_path(url: str | None = None) -> Path | None:
    """Filesystem path of a file-based SQLite URL. In-memory and non-sqlite URLs return None."""
    raw = get_database_url() if url is None else url
    if is_in_memory_sqlite(raw):
        return None
    parsed = make_url(raw)
    if not str(parsed.drivername).startswith("sqlite"):
        return None
    database = parsed.database
    if not database:
        return None
    path = Path(database)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()
