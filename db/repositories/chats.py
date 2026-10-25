from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import TelegramChat
from db.schemas.chat import ChatRow, ChatUpsert


async def upsert_chat(session: AsyncSession, payload: ChatUpsert) -> ChatRow:
    q = await session.scalar(
        select(TelegramChat).where(TelegramChat.telegram_chat_id == payload.telegram_chat_id)
    )
    if q is None:
        row = TelegramChat(
            telegram_chat_id=payload.telegram_chat_id,
            chat_type=payload.chat_type,
            title=payload.title,
        )
        session.add(row)
        await session.flush()
    else:
        q.chat_type = payload.chat_type
        q.title = payload.title
        row = q
        await session.flush()
    await session.refresh(row)
    return ChatRow.model_validate(row)
