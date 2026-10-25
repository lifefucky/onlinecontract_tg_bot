"""Match active onlinecontract sheets to the kinds of goods in the price list.

Dependencies (also listed in requirements.txt):
    xlrd, openai, httpx, python-dotenv, numpy

Run from the repository root:
    python -m matching.run
    MATCH_ALLOW_CATALOG_EMBED=1 python -m matching.run --embed-catalog

The embedding key is loaded from the project ``.env`` and is never printed.
A full price-list embed is not started unless ``--embed-catalog`` is passed
or ``MATCH_ALLOW_CATALOG_EMBED=1``. Output goes to docs/research/matches/run/.
Owner labels in ``match_labels`` are the gold set. The old exact/disputed
jsonl files are a draft of the previous logic and are not scored.
Tender attachments are not downloaded.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

from matching.attributes import false_friend, normalize, parse_line
from matching.catalog import Sku, find_price_xls, load_catalog
from matching.config import EMBED_MODEL, FIT_MIN_COSINE, RUN_DIR, disabled_categories
from matching.embeddings import Embedder, embed_catalog
from matching.errors import CatalogEmbedRequired, PipelineError
from matching.match import build_index, classify_procedure, link_duplicates
from matching.tenders import fetch_positions, fetch_procedures, position_error_ids

logger = logging.getLogger(__name__)


def self_check() -> None:
    assert parse_line("Труба ПВХ гофрированная").killer == "conduit"
    assert parse_line("Услуги клининга по уборке помещений").killer == "service"
    assert false_friend(normalize("Холодильник бытовая техника")) == "appliance"
    assert parse_line("Перчатки х/б с ПВХ").killer is None


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
    path.write_text(payload + ("\n" if payload else ""), encoding="utf-8")


def _brief(record: dict) -> str:
    categories = ", ".join(record.get("categories") or [])
    return (
        f"{record['procedure_id']} {record.get('bucket')} "
        f"{record.get('in_scope')}/{record.get('line_count')} "
        f"cos={record.get('closeness')} [{categories}] {record.get('name', '')[:80]}"
    )


def classify_procedures(
    procedures: list[dict],
    *,
    refresh_positions: bool = False,
    refresh_ids: set[int] | None = None,
    embedder: Embedder | None = None,
    skus: list[Sku] | None = None,
    positions: dict[int, list[dict]] | None = None,
    terms: dict[int, dict[str, str]] | None = None,
    allow_catalog_embed: bool | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Classify the given procedures. An empty list does no work.

    Returns «подходит» sheets, «пограничный» sheets, and every decision including drops.
    """
    if not procedures:
        return [], [], []
    self_check()
    if skus is None:
        price_path = find_price_xls()
        logger.info("price list: %s", Path(price_path).name)
        skus = load_catalog(price_path)
        logger.info("catalog skus=%s", len(skus))
    if positions is None:
        procedure_ids = [int(row["id"]) for row in procedures]
        positions, fetched_terms = fetch_positions(
            procedure_ids,
            refresh=refresh_positions,
            refresh_ids=refresh_ids,
        )
        terms = fetched_terms
    terms = terms or {}

    own_embedder = embedder is None
    if embedder is None:
        embedder = Embedder()
    names = [sku.name for sku in skus]
    embed_catalog(embedder, names, allow_large=allow_catalog_embed)
    extra: list[str] = []
    from matching.categories import NEGATIVE_SAMPLES

    extra.extend(NEGATIVE_SAMPLES)
    for procedure in procedures:
        for position in positions.get(int(procedure["id"]), []):
            extra.append(position["name"])
    vectors = embedder.embed(names + extra)
    if own_embedder:
        logger.info(
            "embeddings model=%s api_calls=%s new_texts=%s",
            EMBED_MODEL,
            embedder.api_calls,
            embedder.new_texts,
        )
    index = build_index(skus, vectors)
    disabled = disabled_categories()

    decisions = []
    for procedure in procedures:
        procedure_id = int(procedure["id"])
        shared = terms.get(procedure_id) or {}
        sheet = {**procedure, **shared} if shared else procedure
        decisions.append(
            classify_procedure(
                sheet,
                positions.get(procedure_id, []),
                index,
                vectors,
                disabled=disabled,
            )
        )
    kept = [record for record in decisions if record.get("bucket") in {"fit", "borderline"}]
    link_duplicates(kept)
    fit = sorted((record for record in kept if record["bucket"] == "fit"), key=lambda record: record["procedure_id"])
    borderline = sorted(
        (record for record in kept if record["bucket"] == "borderline"),
        key=lambda record: record["procedure_id"],
    )
    return fit, borderline, decisions


def collect_matches(*, refresh: bool = False) -> tuple[list[dict], list[dict], list[dict]]:
    """Return fit sheets, borderline sheets, and every decision including drops."""
    return classify_procedures(fetch_procedures(refresh=refresh))


def embed_price_list() -> None:
    """One deliberate embed of every price position. Does not fetch tenders."""
    price_path = find_price_xls()
    skus = load_catalog(price_path)
    logger.info("embedding catalog skus=%s file=%s", len(skus), Path(price_path).name)
    embedder = Embedder()
    embed_catalog(embedder, [sku.name for sku in skus], allow_large=True)
    logger.info("catalog embeddings api_calls=%s new_texts=%s", embedder.api_calls, embedder.new_texts)


def score_owner_labels(labels: list[dict], *, threshold: float = FIT_MIN_COSINE) -> dict[str, int]:
    """Count human «подходит» / «не подходит» against the current mean-cosine threshold.

    Does not change the threshold.
    """
    human_fit = 0
    human_fit_above = 0
    human_reject = 0
    human_reject_below = 0
    for label in labels:
        closeness = label.get("closeness")
        if not isinstance(closeness, (int, float)) or isinstance(closeness, bool):
            continue
        decision = label.get("decision")
        above = float(closeness) >= threshold
        if decision == "fit":
            human_fit += 1
            if above:
                human_fit_above += 1
        elif decision == "reject":
            human_reject += 1
            if not above:
                human_reject_below += 1
    return {
        "human_fit": human_fit,
        "human_fit_above": human_fit_above,
        "human_reject": human_reject,
        "human_reject_below": human_reject_below,
    }


def read_owner_labels(path: Path | None = None) -> list[dict]:
    """Labels stored by the bot. An empty list means there is nothing to score yet."""
    if path is None:
        from misc.db_url import sqlite_file_path

        path = sqlite_file_path()
    if path is None or not path.is_file():
        return []
    try:
        connection = sqlite3.connect(path)
    except sqlite3.Error:
        return []
    try:
        try:
            cursor = connection.execute(
                "SELECT procedure_id, level, lines_json, decision, closeness, "
                "telegram_user_id, answered_at FROM match_labels"
            )
        except sqlite3.OperationalError:
            return []
        labels: list[dict] = []
        for procedure_id, level, lines_json, decision, closeness, user_id, answered_at in cursor:
            try:
                lines = json.loads(lines_json) if lines_json else []
            except json.JSONDecodeError:
                lines = []
            labels.append(
                {
                    "procedure_id": int(procedure_id),
                    "level": level,
                    "lines": lines,
                    "decision": decision,
                    "closeness": closeness,
                    "telegram_user_id": int(user_id),
                    "answered_at": answered_at,
                }
            )
        return labels
    finally:
        connection.close()


def _print_label_score(labels: list[dict], decisions: list[dict]) -> None:
    if not labels:
        print(
            "Меток владельцев нет. Старые exact.jsonl и disputed.jsonl — черновик "
            "прежней логики и в оценку не входят."
        )
        return
    score = score_owner_labels(labels)
    print(
        f"Меток: {len(labels)}. Порог среднего косинуса {FIT_MIN_COSINE}. "
        "Порог сам не подстраивается."
    )
    print(
        f"человек «подходит»: {score['human_fit']}, из них не ниже порога: {score['human_fit_above']}"
    )
    print(
        "человек «не подходит»: "
        f"{score['human_reject']}, из них ниже порога: {score['human_reject_below']}"
    )
    by_id = {int(record["procedure_id"]): record for record in decisions}
    print("== метки на текущем прогоне ==")
    seen: set[tuple[int, int]] = set()
    for label in labels:
        key = (int(label["procedure_id"]), int(label["telegram_user_id"]))
        if key in seen:
            continue
        seen.add(key)
        record = by_id.get(int(label["procedure_id"]))
        if record is None:
            print(label["procedure_id"], label["decision"], "нет в этом прогоне")
            continue
        print(label["procedure_id"], "метка", label["decision"], "сейчас", record.get("bucket"), record.get("closeness"))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Price-list intersection on public procedure text")
    parser.add_argument("--refresh", action="store_true", help="Refetch the active procedure list")
    parser.add_argument(
        "--embed-catalog",
        action="store_true",
        help="Embed every price position once and exit. Does not fetch tenders.",
    )
    args = parser.parse_args()
    try:
        if args.embed_catalog:
            embed_price_list()
            return 0
        fit, borderline, decisions = collect_matches(refresh=args.refresh)
    except CatalogEmbedRequired:
        print(
            "В кэше нет эмбеддингов всего прайса. Запрос к модели не отправлен. "
            "Один раз: MATCH_ALLOW_CATALOG_EMBED=1 python -m matching.run --embed-catalog",
            file=sys.stderr,
        )
        return 1
    except PipelineError as exc:
        print(exc, file=sys.stderr)
        return 1
    fit_path = RUN_DIR / "fit.jsonl"
    borderline_path = RUN_DIR / "borderline.jsonl"
    _write_jsonl(fit_path, fit)
    _write_jsonl(borderline_path, borderline)
    failed = position_error_ids([int(row["procedure_id"]) for row in decisions if row.get("procedure_id")])
    if failed:
        logger.info("position files with errors: %s", sorted(failed))

    print("\n== predicted ==")
    for record in fit + borderline:
        print(_brief(record))
    _print_label_score(read_owner_labels(), decisions)
    print(f"\nwrote {fit_path}")
    print(f"wrote {borderline_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
