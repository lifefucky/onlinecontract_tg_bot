"""Sheet decision: nearest price positions and one category, then one level.

Closeness of a line is the cosine to the nearest price position of the category
that won the neighbor vote. Closeness of a sheet is the mean of those values.
The mean is used, not the max, so one very close line does not hide a sheet of
weak ones. ``FIT_MIN_COSINE`` splits «подходит» and «пограничный». Volume,
weight, pack count, material, coating, and brand are not gates. False friends
still are: the dictionary in ``attributes`` and the negative samples.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

import numpy as np

from matching.attributes import cleanup_embed, false_friend, normalize
from matching.catalog import Sku
from matching.categories import NEGATIVE_SAMPLES
from matching.config import (
    FIT_MIN_COSINE,
    METHOD,
    MIN_CLOSE_NEIGHBORS,
    NEG_MARGIN,
    NEIGHBOR_K,
    NEIGHBOR_MAJORITY,
    NEAR_MIN_COSINE,
    TENDER_LINK,
)
from matching.errors import PipelineError

IN_SCOPE = "in_scope"
OUT_OF_SCOPE = "out_of_scope"
BUCKET_FIT = "fit"
BUCKET_BORDERLINE = "borderline"


@dataclass
class CatalogIndex:
    names: list[str]
    categories: list[str]
    matrix: np.ndarray
    negative: np.ndarray


def build_index(skus: list[Sku], vectors: dict[str, np.ndarray]) -> CatalogIndex:
    rows: list[np.ndarray] = []
    names: list[str] = []
    categories: list[str] = []
    for sku in skus:
        vector = vectors.get(cleanup_embed(sku.name))
        if vector is None:
            continue
        rows.append(vector)
        names.append(sku.name)
        categories.append(sku.category)
    if not rows:
        raise PipelineError("price list has no embedded positions")
    neg_rows: list[np.ndarray] = []
    for text in NEGATIVE_SAMPLES:
        vector = vectors.get(cleanup_embed(text))
        if vector is None:
            raise PipelineError("negative sample has no embedding")
        neg_rows.append(vector)
    return CatalogIndex(
        names=names,
        categories=categories,
        matrix=np.vstack(rows),
        negative=np.vstack(neg_rows),
    )


def _top_indexes(scores: np.ndarray, k: int) -> list[int]:
    count = int(scores.shape[0])
    if count == 0:
        return []
    take = min(k, count)
    if take == count:
        order = np.argsort(scores)[::-1]
        return [int(i) for i in order]
    picked = np.argpartition(scores, -take)[-take:]
    order = np.argsort(scores[picked])[::-1]
    return [int(i) for i in picked[order]]


def _line_record(
    text: str,
    qty: Any,
    unit: Any,
    price: Any,
    *,
    relation: str,
    category: str | None = None,
    score: float | None = None,
    similar: list[str] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {"tender_name": text, "relation": relation}
    if category:
        record["category"] = category
    if qty is not None:
        record["qty"] = qty
    if unit:
        record["unit"] = unit
    if isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0:
        record["price"] = price
    if score is not None:
        record["embed_cosine"] = round(score, 4)
    if similar:
        record["similar"] = similar[:2]
    return record


def analyze_text(
    text: str,
    index: CatalogIndex,
    vectors: dict[str, np.ndarray],
    *,
    disabled: frozenset[str],
    qty: Any = None,
    unit: Any = None,
    price: Any = None,
) -> dict[str, Any]:
    if false_friend(normalize(text)):
        return _line_record(text, qty, unit, price, relation=OUT_OF_SCOPE)
    vector = vectors.get(cleanup_embed(text))
    if vector is None:
        return _line_record(text, qty, unit, price, relation=OUT_OF_SCOPE)

    best_neg = float(np.max(index.negative @ vector))
    scores = index.matrix @ vector
    close: list[tuple[int, float]] = []
    for idx in _top_indexes(scores, NEIGHBOR_K):
        score = float(scores[idx])
        if score < NEAR_MIN_COSINE or score <= best_neg + NEG_MARGIN:
            continue
        close.append((idx, score))
    if len(close) < MIN_CLOSE_NEIGHBORS:
        return _line_record(text, qty, unit, price, relation=OUT_OF_SCOPE)

    counts = Counter(index.categories[idx] for idx, _score in close)
    category, count = counts.most_common(1)[0]
    if count / len(close) < NEIGHBOR_MAJORITY or category.casefold() in disabled:
        return _line_record(text, qty, unit, price, relation=OUT_OF_SCOPE)

    similar: list[str] = []
    best: float | None = None
    for idx, score in close:
        if index.categories[idx] != category:
            continue
        if best is None:
            best = score
        name = index.names[idx]
        if name not in similar:
            similar.append(name)
        if len(similar) == 2:
            break
    if best is None:
        return _line_record(text, qty, unit, price, relation=OUT_OF_SCOPE)
    return _line_record(
        text,
        qty,
        unit,
        price,
        relation=IN_SCOPE,
        category=category,
        score=best,
        similar=similar[:2],
    )


def _sheet_lines(
    positions: list[dict],
    index: CatalogIndex,
    vectors: dict[str, np.ndarray],
    *,
    disabled: frozenset[str],
) -> list[dict]:
    lines: list[dict] = []
    for position in positions:
        lines.append(
            analyze_text(
                position["name"],
                index,
                vectors,
                disabled=disabled,
                qty=position.get("qty"),
                unit=position.get("unit"),
                price=position.get("price"),
            )
        )
    return lines


def _categories(lines: list[dict]) -> list[str]:
    counts = Counter(line.get("category") for line in lines if line.get("relation") == IN_SCOPE)
    counts.pop(None, None)
    return [name for name, _count in counts.most_common()]


def _closeness(lines: list[dict]) -> float | None:
    scores = [
        float(line["embed_cosine"])
        for line in lines
        if line.get("relation") == IN_SCOPE and isinstance(line.get("embed_cosine"), (int, float))
    ]
    if not scores:
        return None
    return float(sum(scores) / len(scores))


def _public_record(procedure: dict, lines: list[dict], bucket: str, closeness: float) -> dict[str, Any]:
    procedure_id = int(procedure["id"])
    owner = procedure.get("owner")
    owner_name = owner.get("name", "") if isinstance(owner, dict) else (owner or "")
    in_scope = [line for line in lines if line.get("relation") == IN_SCOPE]
    record: dict[str, Any] = {
        "method": METHOD,
        "bucket": bucket,
        "procedure_id": procedure_id,
        "link": TENDER_LINK.format(procedure_id=procedure_id),
        "owner": owner_name,
        "name": procedure.get("name") or "",
        "trade_window": {
            "start": procedure.get("reBiddingStart"),
            "end": procedure.get("reBiddingEnd"),
        },
        "start_price": procedure.get("startPrice") or 0,
        "already_shown_in_chat": None,
        "show_ledger": (
            "бот не шлёт повтор: shown_procedures хранит procedure_id по чату. "
            "bot_message_tracks эту рассылку не ведёт"
        ),
        "categories": _categories(lines),
        "in_scope": len(in_scope),
        "line_count": len(lines),
        "closeness": round(closeness, 6),
        "lines": lines,
        "spec_docs": None,
        "note": (
            "Решение по открытым названиям позиций. Файлы ТЗ не скачивались. "
            "Близость — средний косинус к ближайшей позиции выбранной категории "
            "(baai/bge-m3, dense). Объём, вес, фасовка и бренд не учитываются."
        ),
    }
    if "useNDS" in procedure:
        record["useNDS"] = procedure.get("useNDS")
    if "nds" in procedure:
        record["nds"] = procedure.get("nds")
    for key in ("ownerSrok", "ownerSklad"):
        value = procedure.get(key)
        if isinstance(value, str) and value.strip():
            record[key] = value.strip()
    return record


def classify_procedure(
    procedure: dict,
    positions: list[dict],
    index: CatalogIndex,
    vectors: dict[str, np.ndarray],
    *,
    disabled: frozenset[str] | None = None,
) -> dict[str, Any]:
    disabled = disabled if disabled is not None else frozenset()
    lines = _sheet_lines(positions, index, vectors, disabled=disabled)
    closeness = _closeness(lines)
    if closeness is None:
        return {
            "bucket": "drop",
            "procedure_id": int(procedure["id"]),
            "name": procedure.get("name") or "",
            "lines": lines,
            "line_count": len(lines),
            "in_scope": 0,
        }
    bucket = BUCKET_FIT if closeness >= FIT_MIN_COSINE else BUCKET_BORDERLINE
    return _public_record(procedure, lines, bucket, closeness)


def link_duplicates(records: list[dict]) -> None:
    groups: dict[tuple, list[dict]] = {}
    for record in records:
        if record.get("bucket") not in {BUCKET_FIT, BUCKET_BORDERLINE}:
            continue
        matched = tuple(
            sorted(
                normalize(line["tender_name"])
                for line in record.get("lines") or []
                if line.get("relation") == IN_SCOPE
            )
        )
        if not matched:
            continue
        window = record.get("trade_window") or {}
        key = (record.get("owner"), window.get("start"), window.get("end"), matched)
        groups.setdefault(key, []).append(record)
    for group in groups.values():
        if len(group) < 2:
            continue
        ids = sorted(item["procedure_id"] for item in group)
        for item in group:
            others = [procedure_id for procedure_id in ids if procedure_id != item["procedure_id"]]
            item["possible_duplicate_of"] = others[0]
