"""Offers shown to owners and the answers that become the gold set."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.match_label import MatchLabel, MatchOffer


def _in_scope_lines(record: dict) -> list[dict]:
    lines = []
    for line in record.get("lines") or []:
        if line.get("relation") != "in_scope":
            continue
        lines.append(
            {
                "tender_name": line.get("tender_name"),
                "category": line.get("category"),
                "embed_cosine": line.get("embed_cosine"),
                "similar": list(line.get("similar") or [])[:2],
                "qty": line.get("qty"),
                "unit": line.get("unit"),
            }
        )
    return lines


async def save_offer(session: AsyncSession, record: dict, *, content_hash: str) -> None:
    """Remember the judgement on the card. The same hash updates the same row."""
    procedure_id = int(record["procedure_id"])
    lines = _in_scope_lines(record)
    closeness = record.get("closeness")
    if not isinstance(closeness, (int, float)) or isinstance(closeness, bool):
        closeness = 0.0
    payload = json.dumps(lines, ensure_ascii=False)
    existing = await session.scalar(
        select(MatchOffer).where(
            MatchOffer.procedure_id == procedure_id,
            MatchOffer.content_hash == content_hash,
        )
    )
    if existing is None:
        session.add(
            MatchOffer(
                procedure_id=procedure_id,
                content_hash=content_hash,
                level=str(record.get("bucket") or ""),
                lines_json=payload,
                closeness=float(closeness),
                in_scope_count=int(record.get("in_scope") or len(lines)),
                line_count=int(record.get("line_count") or 0),
            )
        )
    else:
        existing.level = str(record.get("bucket") or "")
        existing.lines_json = payload
        existing.closeness = float(closeness)
        existing.in_scope_count = int(record.get("in_scope") or len(lines))
        existing.line_count = int(record.get("line_count") or 0)
    await session.flush()


async def record_feedback(
    session: AsyncSession,
    *,
    procedure_id: int,
    content_token: str,
    decision: str,
    telegram_user_id: int,
) -> bool:
    """Store the owner's answer for the offer whose hash starts with ``content_token``.

    Returns False when that card is no longer in the database.
    """
    offers = (
        await session.scalars(
            select(MatchOffer)
            .where(MatchOffer.procedure_id == procedure_id)
            .order_by(MatchOffer.id.desc())
        )
    ).all()
    offer = next((row for row in offers if row.content_hash.startswith(content_token)), None)
    if offer is None:
        return False
    existing = await session.scalar(
        select(MatchLabel).where(
            MatchLabel.procedure_id == procedure_id,
            MatchLabel.telegram_user_id == telegram_user_id,
            MatchLabel.content_hash == offer.content_hash,
        )
    )
    now = datetime.now(timezone.utc)
    if existing is None:
        session.add(
            MatchLabel(
                procedure_id=procedure_id,
                content_hash=offer.content_hash,
                level=offer.level,
                lines_json=offer.lines_json,
                closeness=offer.closeness,
                decision=decision,
                telegram_user_id=telegram_user_id,
                answered_at=now,
            )
        )
    else:
        existing.level = offer.level
        existing.lines_json = offer.lines_json
        existing.closeness = offer.closeness
        existing.decision = decision
        existing.answered_at = now
    await session.flush()
    return True
