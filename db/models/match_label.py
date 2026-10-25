from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class MatchOffer(Base):
    """The judgement that was put on an owner card, so a button can store the same lines.

    ``content_hash`` is the hash of that card. The callback carries its first
    characters. A later re-match writes a new row and leaves the old card's row.
    """

    __tablename__ = "match_offers"
    __table_args__ = (
        UniqueConstraint("procedure_id", "content_hash", name="uq_match_offer_procedure_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    procedure_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    lines_json: Mapped[str] = mapped_column(Text, nullable=False)
    closeness: Mapped[float] = mapped_column(Float, nullable=False)
    in_scope_count: Mapped[int] = mapped_column(Integer, nullable=False)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MatchLabel(Base):
    """One owner's answer on a sheet. These rows are the gold set for the threshold."""

    __tablename__ = "match_labels"
    __table_args__ = (
        UniqueConstraint(
            "procedure_id",
            "telegram_user_id",
            "content_hash",
            name="uq_match_label_procedure_user_hash",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    procedure_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    lines_json: Mapped[str] = mapped_column(Text, nullable=False)
    closeness: Mapped[float] = mapped_column(Float, nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    answered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
