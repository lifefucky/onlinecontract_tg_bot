"""Shown competitive sheets: one row per chat, procedure, and content hash.

Revision ID: 003_shown_procedures
Revises: 002_telegram_users
Create Date: 2026-10-24

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_shown_procedures"
down_revision: Union[str, None] = "002_telegram_users"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shown_procedures",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("procedure_id", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("bucket", sa.String(length=16), nullable=False),
        sa.Column(
            "posted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "telegram_chat_id",
            "procedure_id",
            "content_hash",
            name="uq_shown_chat_procedure_hash",
        ),
    )
    op.create_index(
        "ix_shown_procedures_telegram_chat_id",
        "shown_procedures",
        ["telegram_chat_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_shown_procedures_telegram_chat_id", table_name="shown_procedures")
    op.drop_table("shown_procedures")
