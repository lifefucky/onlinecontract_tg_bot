from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class ShownProcedure(Base):
    """A competitive sheet already posted to one Telegram chat.

    Any row for ``(telegram_chat_id, procedure_id)`` means that chat was sent
    this procedure. ``content_hash`` is the composition at send time.
    """

    __tablename__ = "shown_procedures"
    __table_args__ = (
        UniqueConstraint(
            "telegram_chat_id",
            "procedure_id",
            "content_hash",
            name="uq_shown_chat_procedure_hash",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    procedure_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    bucket: Mapped[str] = mapped_column(String(16), nullable=False)
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
