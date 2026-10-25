"""Owner labels on price-list judgements.

Revision ID: 004_match_labels
Revises: 003_shown_procedures
Create Date: 2026-10-25

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004_match_labels"
down_revision: Union[str, None] = "003_shown_procedures"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "match_offers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("procedure_id", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("lines_json", sa.Text(), nullable=False),
        sa.Column("closeness", sa.Float(), nullable=False),
        sa.Column("in_scope_count", sa.Integer(), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("procedure_id", "content_hash", name="uq_match_offer_procedure_hash"),
    )
    op.create_index("ix_match_offers_procedure_id", "match_offers", ["procedure_id"], unique=False)

    op.create_table(
        "match_labels",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("procedure_id", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("lines_json", sa.Text(), nullable=False),
        sa.Column("closeness", sa.Float(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "answered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "procedure_id",
            "telegram_user_id",
            "content_hash",
            name="uq_match_label_procedure_user_hash",
        ),
    )
    op.create_index("ix_match_labels_procedure_id", "match_labels", ["procedure_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_match_labels_procedure_id", table_name="match_labels")
    op.drop_table("match_labels")
    op.drop_index("ix_match_offers_procedure_id", table_name="match_offers")
    op.drop_table("match_offers")
