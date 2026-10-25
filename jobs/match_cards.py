"""Text and keyboard for a price-list intersection card."""

from __future__ import annotations

import hashlib
import json
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

MATCHED_RELATIONS = frozenset({"in_scope"})
BUCKET_FIT = "fit"
BUCKET_BORDERLINE = "borderline"
BUCKET_TITLE = {
    BUCKET_FIT: "Подходит",
    BUCKET_BORDERLINE: "Пограничный",
}


def matched_lines(record: dict) -> list[dict]:
    return [line for line in record.get("lines") or [] if line.get("relation") in MATCHED_RELATIONS]


def content_hash(record: dict) -> str:
    """Stable hash of the level and the in-scope lines, including the similar positions."""
    payload = {
        "bucket": record.get("bucket"),
        "lines": [
            {
                "category": line.get("category"),
                "name": line.get("tender_name"),
                "qty": line.get("qty"),
                "relation": line.get("relation"),
                "similar": line.get("similar"),
                "unit": line.get("unit"),
            }
            for line in matched_lines(record)
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _line_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _nds_value(record: dict) -> str | None:
    """Same rule as ``beautiful_procedure``: a rate when ``useNDS`` is set, otherwise «Нет»."""
    if "useNDS" not in record:
        return None
    if record.get("useNDS"):
        nds = record.get("nds")
        if nds is None:
            return None
        text = str(nds).strip()
        if not text:
            return None
        return f"{text} %"
    return "Нет"


def _field(label: str, value: str) -> str:
    return f"<b>{escape(label)}:</b> {escape(value)}"


def _procedure_card(record: dict) -> str:
    """Owner link, name, VAT, and the payment/delivery block. Missing values are left out."""
    procedure_id = int(record["procedure_id"])
    owner = _line_value(record.get("owner"))
    link = _line_value(record.get("link")) or f"https://onlinecontract.ru/tenders/{procedure_id}"
    title = f"{owner}[{procedure_id}]" if owner else f"[{procedure_id}]"
    lines = [f'<a href="{escape(link, quote=True)}">{escape(title)}</a>']
    name = _line_value(record.get("name"))
    if name:
        lines.append(_field("Наименование", name))
    nds = _nds_value(record)
    if nds:
        lines.append(_field("НДС", nds))
    payment = _line_value(record.get("ownerSrok"))
    delivery = _line_value(record.get("ownerSklad"))
    if payment or delivery:
        lines.append("")
        lines.append("Условия:")
        if payment:
            lines.append(_field("Условия оплаты", payment))
        if delivery:
            lines.append(_field("Сроки поставки", delivery))
    return "\n".join(lines)


def _similar_text(line: dict) -> str:
    names = [str(name).strip() for name in (line.get("similar") or []) if str(name).strip()]
    shown = names[:2]
    if not shown:
        return ""
    joined = "»; «".join(escape(name) for name in shown)
    return f" — похоже на «{joined}»"


def _admin_note(record: dict) -> str:
    """Level, categories, how many lines are in scope, and one or two similar positions."""
    lines: list[str] = []
    bucket = str(record.get("bucket") or "")
    title = BUCKET_TITLE.get(bucket)
    if title:
        lines.append(f"<b>{escape(title)}</b>")
    categories = [str(name).strip() for name in (record.get("categories") or []) if str(name).strip()]
    if categories:
        lines.append(_field("Категории", ", ".join(categories)))
    in_scope = record.get("in_scope")
    total = record.get("line_count")
    if isinstance(in_scope, int) and isinstance(total, int) and not isinstance(in_scope, bool):
        lines.append(f"<b>В наших категориях:</b> {in_scope} из {total}")
    duplicate = record.get("possible_duplicate_of")
    if duplicate:
        lines.append(f"<b>Похожий лист:</b> {int(duplicate)}")
    interesting = matched_lines(record)
    if interesting:
        shown = interesting[:12]
        for line in shown:
            name = escape(str(line.get("tender_name") or ""))
            lines.append(f"• {name}{_qty(line)}{_similar_text(line)}")
        hidden = len(interesting) - len(shown)
        if hidden > 0:
            lines.append(f"ещё {hidden}")
    return "\n".join(lines)


def _qty(line: dict) -> str:
    qty = line.get("qty")
    unit = line.get("unit") or ""
    if qty is None or qty == "":
        return ""
    if isinstance(qty, float) and qty.is_integer():
        qty = int(qty)
    text = f"{qty} {unit}".strip()
    return f" — {escape(text)}"


def format_match_card(record: dict, *, for_admin: bool = False) -> str:
    """Procedure card. Owners also see the level, the categories, and similar positions."""
    body = _procedure_card(record)
    if not for_admin:
        return body
    note = _admin_note(record)
    if not note:
        return body
    return f"{body}\n\n{note}"


def _card_keyboard(procedure_id: int, *, for_admin: bool, content_hash: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="Позиции", callback_data=f"pos_{procedure_id}")]
    ]
    if for_admin:
        token = content_hash[:8]
        rows.append(
            [
                InlineKeyboardButton(text="Подходит", callback_data=f"mf|{procedure_id}|1|{token}"),
                InlineKeyboardButton(text="Не подходит", callback_data=f"mf|{procedure_id}|0|{token}"),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)
