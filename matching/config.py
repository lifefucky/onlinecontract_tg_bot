"""Paths, model id, and the «подходит» / «пограничный» threshold.

The embedding key is read from the project ``.env`` (see ``misc.load_env``).
``docs/research/.env`` fills only variables that are still empty. This module
never prints ``OPENAI_API_KEY``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from misc.load_env import load_project_env

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
RESEARCH_DIR = REPO_ROOT / "docs" / "research"
RUN_DIR = RESEARCH_DIR / "matches" / "run"

load_project_env()

OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.proxyapi.ru/v1")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")


def resolve_cache_dir() -> Path:
    """Prefer ``matching/cache``. Reuse the old research cache when that is the one on disk."""
    new_dir = PACKAGE_DIR / "cache"
    old_dir = RESEARCH_DIR / "pipeline" / "cache"
    if (new_dir / "embeddings_meta.json").is_file():
        return new_dir
    if (old_dir / "embeddings_meta.json").is_file():
        return old_dir
    return new_dir


CACHE_DIR = resolve_cache_dir()

# ProxyAPI exposes BGE-M3 as an OpenAI-compatible dense embedding.
EMBED_MODEL = "baai/bge-m3"
EMBED_BATCH = 64

PROCEDURES_URL = "https://api.onlc.ru/purchases/v1/public/procedures"
POSITIONS_URL = "https://api.onlc.ru/purchases/v1/public/procedures/{procedure_id}/positions"
TENDER_LINK = "https://onlinecontract.ru/tenders/{procedure_id}"

METHOD = "public_text_bge_m3_category_nn"

# A price position counts as a neighbor when its cosine reaches this floor.
# 0.58 is the old near-match floor. It is not an attribute threshold.
NEAR_MIN_COSINE = 0.58
# Sheet level is «подходит» when the mean cosine of in-scope lines to the
# nearest price position of their category reaches this floor. Otherwise, if
# any line is in scope, the sheet is «пограничный». 0.74 is the old exact-match
# floor, kept as a starting point until owner labels tune it.
FIT_MIN_COSINE = 0.74
# A neighbor must beat the nearest false-friend sample by this margin.
NEG_MARGIN = 0.02
NEIGHBOR_K = 5
# The leading category must cover at least this share of the close neighbors.
NEIGHBOR_MAJORITY = 0.6
MIN_CLOSE_NEIGHBORS = 2

# Warn owners when the price file is older than this. Matching still runs.
PRICE_MAX_AGE_DAYS = 120
# Re-read tender positions at most this often while the procedure stays active.
POSITION_REFRESH_SECONDS = 24 * 60 * 60

FETCH_WORKERS = 8
FETCH_TIMEOUT = 45.0


def disabled_categories() -> frozenset[str]:
    """Category names turned off via ``MATCH_DISABLED_CATEGORIES``. Default: none."""
    raw = os.environ.get("MATCH_DISABLED_CATEGORIES", "")
    return frozenset(part.strip().casefold() for part in raw.split(",") if part.strip())


def catalog_embed_allowed(explicit: bool | None = None) -> bool:
    """Full price-list embed runs only when this is true. The bot leaves it off."""
    if explicit is not None:
        return explicit
    return os.environ.get("MATCH_ALLOW_CATALOG_EMBED", "").strip() == "1"


def rules_version() -> str:
    """Changes when categories, the enabled set, thresholds, or false friends change."""
    from matching.attributes import killer_sources
    from matching.categories import NEGATIVE_SAMPLES, category_signature

    payload = {
        "categories": category_signature(),
        "disabled": sorted(disabled_categories()),
        "fit_min_cosine": FIT_MIN_COSINE,
        "near_min_cosine": NEAR_MIN_COSINE,
        "neg_margin": NEG_MARGIN,
        "neighbor_k": NEIGHBOR_K,
        "neighbor_majority": NEIGHBOR_MAJORITY,
        "min_close_neighbors": MIN_CLOSE_NEIGHBORS,
        "killers": killer_sources(),
        "negatives": NEGATIVE_SAMPLES,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
