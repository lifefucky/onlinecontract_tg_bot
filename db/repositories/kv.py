from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AppKvStore

DIALOG_RESET_ALL_KEY = "dialog_reset_all_v1"


async def get_bool(session: AsyncSession, key: str) -> bool:
    row = await session.get(AppKvStore, key)
    if row is None:
        return False
    try:
        return bool(json.loads(row.value_json))
    except json.JSONDecodeError:
        return False


async def set_bool(session: AsyncSession, key: str, value: bool) -> None:
    payload = json.dumps(value)
    row = await session.get(AppKvStore, key)
    if row is None:
        session.add(AppKvStore(key=key, value_json=payload))
    else:
        row.value_json = payload
    await session.flush()


async def get_json(session: AsyncSession, key: str) -> object | None:
    """Decoded JSON for ``key``, or None when the key is absent."""
    row = await session.get(AppKvStore, key)
    if row is None:
        return None
    return json.loads(row.value_json)


async def set_json(session: AsyncSession, key: str, value: object) -> None:
    payload = json.dumps(value, ensure_ascii=False)
    row = await session.get(AppKvStore, key)
    if row is None:
        session.add(AppKvStore(key=key, value_json=payload))
    else:
        row.value_json = payload
    await session.flush()
