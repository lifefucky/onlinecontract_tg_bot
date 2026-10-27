"""Fetch active procedures, public position names, and shared sheet terms. No attachment downloads."""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from onlinecontract.client import HTTPError, POSITIONS_URL, PROCEDURES_URL, api_client, api_get
from matching.config import CACHE_DIR, FETCH_TIMEOUT, FETCH_WORKERS

logger = logging.getLogger(__name__)


def _cached_file(relative: str) -> Path:
    """File under ``matching/cache`` for procedure and position snapshots.

    Runtime code no longer falls back to ``docs/research``; a redirected
    ``CACHE_DIR`` in tests is honoured as-is.
    """
    return CACHE_DIR / relative


def _procedures_path() -> Path:
    return _cached_file("procedures.json")


def fetch_procedures(refresh: bool = False, *, progress: bool = True) -> list[dict]:
    def say(message: str) -> None:
        if progress:
            logger.info(message)

    path = _procedures_path()
    if path.is_file() and not refresh:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(cached, list) and len(cached) >= 400:
            say(f"procedures cache {len(cached)}")
            return cached
    rows: list[dict] = []
    offset = 0
    total: int | None = None
    with api_client(FETCH_TIMEOUT) as client:
        while True:
            url = (
                f"{PROCEDURES_URL}?filters[status]=1&limit=100&offset={offset}"
                "&include=owner&total=true"
            )
            response = api_get(url, client=client)
            response.raise_for_status()
            payload = response.json()
            batch = payload.get("data") or []
            total = (payload.get("meta") or {}).get("total", total)
            say(f"procedures offset={offset} batch={len(batch)} total={total}")
            if not batch:
                if total is not None and offset < int(total):
                    raise RuntimeError(f"procedure list truncated at offset {offset} of {total}")
                break
            rows.extend(batch)
            offset += 100
            if total is not None and offset >= int(total):
                break
    unique: list[dict] = []
    seen: set[int] = set()
    for row in rows:
        procedure_id = row.get("id")
        if procedure_id in seen:
            continue
        seen.add(procedure_id)
        unique.append(row)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / "procedures.json").write_text(json.dumps(unique, ensure_ascii=False), encoding="utf-8")
    say(f"procedures fetched {len(unique)}")
    return unique


def cached_procedure_ids() -> set[int] | None:
    """Ids from the last procedure-list file, or None when that file cannot be used."""
    path = _procedures_path()
    if not path.is_file():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cached, list):
        return None
    ids: set[int] = set()
    for row in cached:
        if not isinstance(row, dict):
            continue
        procedure_id = row.get("id")
        if isinstance(procedure_id, bool) or not isinstance(procedure_id, int):
            continue
        ids.add(procedure_id)
    return ids or None


def _fetch_one(procedure_id: int, *, refresh: bool = False) -> None:
    relative = f"positions/{procedure_id}.json"
    destination = CACHE_DIR / relative
    if not refresh and _cached_file(relative).is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = POSITIONS_URL.format(procedure_id=procedure_id)
    last_error = "no response"
    with api_client(FETCH_TIMEOUT) as client:
        for attempt in range(4):
            try:
                response = api_get(url, client=client)
            except HTTPError as exc:
                last_error = type(exc).__name__
                continue
            if response.status_code == 200:
                destination.write_text(response.text, encoding="utf-8")
                return
            last_error = f"HTTP {response.status_code}"
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
    destination.write_text(json.dumps({"error": last_error, "data": []}, ensure_ascii=False), encoding="utf-8")


def _term_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def shared_sheet_terms(rows: list) -> dict[str, str]:
    """Payment and delivery text when every position row carries the same value.

    ``ownerSrok`` and ``ownerSklad`` are the fields the procedure card labels
    «Условия оплаты» and «Сроки поставки». A blank or disagreeing value is omitted.
    """
    keys = ("ownerSrok", "ownerSklad")
    usable = [row for row in rows if isinstance(row, dict)]
    if not usable:
        return {}
    shared: dict[str, str] = {}
    for key in keys:
        values = [_term_text(row.get(key)) for row in usable]
        if len(set(values)) == 1 and values[0]:
            shared[key] = values[0]
    return shared


def stale_position_ids(
    procedure_ids: list[int],
    *,
    max_age_seconds: float,
    now: float | None = None,
) -> list[int]:
    """Ids whose position file is missing or older than ``max_age_seconds``.

    A procedure that stays active is re-read at most once per that window.
    """
    moment = time.time() if now is None else now
    stale: list[int] = []
    for procedure_id in procedure_ids:
        path = _cached_file(f"positions/{procedure_id}.json")
        if not path.is_file():
            stale.append(procedure_id)
            continue
        if moment - path.stat().st_mtime >= max_age_seconds:
            stale.append(procedure_id)
    return stale


def fetch_positions(
    procedure_ids: list[int],
    *,
    refresh: bool = False,
    refresh_ids: set[int] | None = None,
) -> tuple[dict[int, list[dict]], dict[int, dict[str, str]]]:
    def _have(procedure_id: int) -> bool:
        return _cached_file(f"positions/{procedure_id}.json").is_file()

    if refresh:
        missing = list(procedure_ids)
    elif refresh_ids is not None:
        wanted = set(refresh_ids)
        missing = [
            procedure_id
            for procedure_id in procedure_ids
            if procedure_id in wanted or not _have(procedure_id)
        ]
    else:
        missing = [procedure_id for procedure_id in procedure_ids if not _have(procedure_id)]
    if missing:
        logger.info("fetching positions %s", len(missing))
        done = 0
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            futures = [
                pool.submit(_fetch_one, procedure_id, refresh=procedure_id in missing)
                for procedure_id in missing
            ]
            for future in as_completed(futures):
                future.result()
                done += 1
                if done % 50 == 0 or done == len(missing):
                    logger.info("positions %s/%s", done, len(missing))
    loaded: dict[int, list[dict]] = {}
    terms: dict[int, dict[str, str]] = {}
    for procedure_id in procedure_ids:
        payload = json.loads(_cached_file(f"positions/{procedure_id}.json").read_text(encoding="utf-8"))
        rows = payload.get("data") if isinstance(payload, dict) else None
        row_list = [row for row in rows or [] if isinstance(row, dict)]
        terms[procedure_id] = shared_sheet_terms(row_list)
        positions = []
        for row in row_list:
            name = row.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            positions.append(
                {
                    "name": name.strip(),
                    "qty": row.get("totalCount"),
                    "unit": row.get("unit"),
                    "price": row.get("price"),
                }
            )
        loaded[procedure_id] = positions
    return loaded, terms


def position_error_ids(procedure_ids: list[int]) -> set[int]:
    """Ids whose position file is missing or records a failed download."""
    failed: set[int] = set()
    for procedure_id in procedure_ids:
        path = _cached_file(f"positions/{procedure_id}.json")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failed.add(procedure_id)
            continue
        if not isinstance(payload, dict) or payload.get("error"):
            failed.add(procedure_id)
    return failed
