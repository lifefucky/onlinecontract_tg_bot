from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ShownProcedure


async def known_keys(session: AsyncSession, telegram_chat_id: int) -> set[int]:
    """Procedure ids this chat was already sent. The stored hash does not matter."""
    rows = await session.execute(
        select(ShownProcedure.procedure_id).where(
            ShownProcedure.telegram_chat_id == telegram_chat_id
        )
    )
    return {int(procedure_id) for procedure_id in rows.scalars().all()}


async def mark_shown(
    session: AsyncSession,
    *,
    telegram_chat_id: int,
    procedure_id: int,
    content_hash: str,
    bucket: str,
) -> None:
    session.add(
        ShownProcedure(
            telegram_chat_id=telegram_chat_id,
            procedure_id=procedure_id,
            content_hash=content_hash,
            bucket=bucket,
        )
    )
    await session.flush()
