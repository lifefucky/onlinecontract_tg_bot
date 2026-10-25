"""Category assignment, neighbor judgement, price age, and owner labels. No live embed."""

import json
import os
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import select

from db.models.match_label import MatchLabel
from db.repositories import labels
from db.session import bind_engine, create_async_engine_from_url, get_session_maker, init_db_schema, reset_engine
from intersection_feed import select_procedures_to_match, should_rematch_all
from matching.catalog import price_file_day, price_fingerprint, skus_from_rows, stale_price_warning
from matching.categories import category_of
from matching.config import EMBED_BATCH, FIT_MIN_COSINE, PRICE_MAX_AGE_DAYS
from matching.embeddings import embed_catalog
from matching.errors import CatalogEmbedRequired
from matching.match import (
    CatalogIndex,
    build_index,
    classify_procedure,
)
from matching.run import read_owner_labels, score_owner_labels
from matching.tenders import stale_position_ids


def _unit(*values: float) -> np.ndarray:
    vector = np.asarray(values, dtype=np.float32)
    return vector / float(np.linalg.norm(vector))


def test_category_of_names_kinds_and_ignores_supplier_words():
    assert category_of("Перчатки х/б с ПВХ") == "перчатки"
    assert category_of("Мешки для мусора 60л") == "пакеты и мешки"
    assert category_of("Стакан прозрачный 200 мл") == "посуда одноразовая"
    assert category_of("Туалетная бумага VEIRO 2сл") == "бумажная продукция"
    assert category_of("Аэрозоль от комаров 100мл") == "средства от насекомых"
    assert category_of("НЭФИС") is None
    assert category_of("Henkel") is None
    assert category_of("Артпласт") is None


def test_headers_name_kinds_and_sheet_titles_do_not():
    rows = [
        ("НЭФИС", ["Моющие средства"], [], None),
        ("НЭФИС", ["AOS Color Automat 3кг"], [10.0], None),
        ("НЭФИС", ["10 класс вязки"], [], None),
        ("НЭФИС", ["ЮНИЛЕВЕР", "ДОМЕСТОС 1л лимон"], [12.0], None),
        ("Посуда", ["Пластиковые стаканы"], [], None),
        ("Посуда", ["100 мл Стакан прозрачный"], [0.4], None),
        ("ЙОРК", ["Щетка для улицы 30 см", "Губки для посуды 5 шт"], [12.0, 50.0], None),
        ("Henkel", ["Средства для ухода за волосами", "Шампунь 380 мл"], [114.0], None),
    ]
    skus = skus_from_rows(rows)
    by_name = {sku.name: sku.category for sku in skus}
    assert by_name["AOS Color Automat 3кг"] == "средства для стирки"
    assert "ЮНИЛЕВЕР" not in by_name
    assert by_name["ДОМЕСТОС 1л лимон"] == "чистящие средства"
    assert by_name["100 мл Стакан прозрачный"] == "посуда одноразовая"
    assert by_name["Щетка для улицы 30 см"] == "уборочный инвентарь"
    assert by_name["Губки для посуды 5 шт"] == "уборочный инвентарь"
    assert by_name["Шампунь 380 мл"] == "уход за волосами"
    assert "НЭФИС" not in set(by_name.values())
    assert "Henkel" not in set(by_name.values())
    assert "Посуда" not in set(by_name.values())


def test_price_date_and_stale_warning(tmp_path):
    dated = tmp_path / "Прайс 26.04. АКЦИИ.xls"
    dated.write_bytes(b"price")
    today = date(2026, 10, 25)
    assert price_file_day(dated, today=today) == date(2026, 4, 26)
    future = tmp_path / "Прайс 26.11.xls"
    future.write_bytes(b"x")
    assert price_file_day(future, today=today) == date(2025, 11, 26)
    plain = tmp_path / "price.xls"
    plain.write_bytes(b"x")
    stamp = datetime(2026, 9, 1, 12, 0).timestamp()
    os.utime(plain, (stamp, stamp))
    assert price_file_day(plain, today=today) == date(2026, 9, 1)
    warning = stale_price_warning(dated, today=today, max_age_days=PRICE_MAX_AGE_DAYS)
    assert warning is not None
    assert "Сопоставление продолжается" in warning
    assert stale_price_warning(plain, today=today, max_age_days=PRICE_MAX_AGE_DAYS) is None
    left = tmp_path / "a.xls"
    right = tmp_path / "b.xls"
    left.write_bytes(b"one")
    right.write_bytes(b"two")
    assert price_fingerprint(left) != price_fingerprint(right)


def test_rules_version_tracks_threshold_and_disabled(monkeypatch):
    from matching import config

    first = config.rules_version()
    monkeypatch.setattr(config, "FIT_MIN_COSINE", FIT_MIN_COSINE + 0.01)
    assert config.rules_version() != first
    monkeypatch.setattr(config, "FIT_MIN_COSINE", FIT_MIN_COSINE)
    monkeypatch.setenv("MATCH_DISABLED_CATEGORIES", "перчатки")
    assert config.rules_version() != first
    monkeypatch.setenv("MATCH_DISABLED_CATEGORIES", "")
    assert config.rules_version() == first


def test_should_rematch_and_stale_positions():
    assert should_rematch_all(None, None, "fp", "rules", has_snapshot=False) is False
    assert should_rematch_all(None, None, "fp", "rules", has_snapshot=True) is True
    assert should_rematch_all("fp", "rules", "fp", "rules", has_snapshot=True) is False
    assert should_rematch_all("old", "rules", "fp", "rules", has_snapshot=True) is True
    assert should_rematch_all("fp", "old", "fp", "rules", has_snapshot=True) is True
    procedures = [{"id": 1}, {"id": 2}, {"id": 3}]
    assert [row["id"] for row in select_procedures_to_match(procedures, {1, 2}, rematch_all=False, stale_ids=set())] == [3]
    again = select_procedures_to_match(procedures, {1, 2}, rematch_all=False, stale_ids={2})
    assert [row["id"] for row in again] == [2, 3]
    assert [row["id"] for row in select_procedures_to_match(procedures, {1}, rematch_all=True, stale_ids=set())] == [1, 2, 3]


def test_stale_position_ids_use_mtime(tmp_path, monkeypatch):
    import matching.tenders as tenders

    monkeypatch.setattr(tenders, "CACHE_DIR", tmp_path)
    folder = tmp_path / "positions"
    folder.mkdir()
    fresh = folder / "1.json"
    old = folder / "2.json"
    fresh.write_text("{}", encoding="utf-8")
    old.write_text("{}", encoding="utf-8")
    now = 1_700_000_000.0
    os.utime(fresh, (now - 100, now - 100))
    os.utime(old, (now - 90_000, now - 90_000))
    assert tenders.stale_position_ids([1, 2, 3], max_age_seconds=86_400, now=now) == [2, 3]
    assert stale_position_ids([1], max_age_seconds=86_400, now=now) == []


def _index(skus, vectors) -> CatalogIndex:
    return build_index(skus, vectors)


def _sku(name: str, category: str):
    from matching.catalog import Sku

    return Sku(name=name, name_norm=name.lower(), sheet="поставщик", ean=None, price=1.0, category=category)


def test_neighbor_vote_ignores_attributes_and_keeps_false_friends():
    glove = _unit(1, 0, 0, 0)
    soap = _unit(0, 1, 0, 0)
    negative = _unit(0, 0, 0, 1)
    # Cosine to the soap positions is about 0.62: above the neighbor floor, below «подходит».
    weak = _unit(0.7846, 0.62, 0, 0)
    glove_skus = [
        _sku("Перчатки х/б 10 класс", "перчатки"),
        _sku("Перчатки х/б эконом", "перчатки"),
        _sku("Перчатки х/б стандарт", "перчатки"),
    ]
    soap_skus = [
        _sku("Мыло 5 л", "мыло"),
        _sku("Мыло 0.5 л", "мыло"),
    ]
    from matching.categories import NEGATIVE_SAMPLES

    vectors = {sku.name: glove for sku in glove_skus}
    vectors.update({sku.name: soap for sku in soap_skus})
    for text in NEGATIVE_SAMPLES:
        vectors[text] = negative
    vectors["Перчатки любые 500 шт"] = glove
    vectors["Мыло хозяйственное без бренда"] = weak
    vectors["Услуги клининга по уборке помещений"] = glove
    procedure = {"id": 7, "name": "лист", "owner": "заказчик"}

    fit = classify_procedure(
        procedure,
        [{"name": "Перчатки любые 500 шт", "qty": 10, "unit": "шт"}],
        _index(glove_skus, vectors),
        vectors,
        disabled=frozenset(),
    )
    assert fit["bucket"] == "fit"
    assert fit["categories"] == ["перчатки"]
    assert fit["in_scope"] == 1
    assert fit["line_count"] == 1
    assert fit["closeness"] >= FIT_MIN_COSINE
    assert fit["lines"][0]["similar"][0].startswith("Перчатки")

    borderline = classify_procedure(
        procedure,
        [{"name": "Мыло хозяйственное без бренда"}, {"name": "Мыло хозяйственное без бренда"}],
        _index(soap_skus, vectors),
        vectors,
        disabled=frozenset(),
    )
    assert borderline["bucket"] == "borderline"
    assert borderline["categories"] == ["мыло"]
    assert borderline["closeness"] < FIT_MIN_COSINE

    killed = classify_procedure(
        procedure,
        [{"name": "Услуги клининга по уборке помещений"}],
        _index(glove_skus, vectors),
        vectors,
        disabled=frozenset(),
    )
    assert killed["bucket"] == "drop"

    disabled = classify_procedure(
        procedure,
        [{"name": "Перчатки любые 500 шт"}],
        _index(glove_skus, vectors),
        vectors,
        disabled=frozenset({"перчатки"}),
    )
    assert disabled["bucket"] == "drop"


def test_split_neighbors_and_a_single_neighbor_stay_out():
    glove = _unit(1, 0, 0)
    bag = _unit(0, 1, 0)
    mixed = _unit(1, 1, 0)
    negative = _unit(0, 0, 1)
    from matching.categories import NEGATIVE_SAMPLES

    skus = [
        _sku("Перчатки а", "перчатки"),
        _sku("Перчатки б", "перчатки"),
        _sku("Мешок а", "пакеты и мешки"),
        _sku("Мешок б", "пакеты и мешки"),
    ]
    vectors = {
        "Перчатки а": glove,
        "Перчатки б": glove,
        "Мешок а": bag,
        "Мешок б": bag,
        "Смешанная строка": mixed,
        "Одиночная": glove,
    }
    for text in NEGATIVE_SAMPLES:
        vectors[text] = negative
    split = classify_procedure(
        {"id": 1, "name": ""},
        [{"name": "Смешанная строка"}],
        build_index(skus, vectors),
        vectors,
        disabled=frozenset(),
    )
    assert split["bucket"] == "drop"
    alone = classify_procedure(
        {"id": 2, "name": ""},
        [{"name": "Одиночная"}],
        build_index([skus[0]], vectors),
        vectors,
        disabled=frozenset(),
    )
    assert alone["bucket"] == "drop"


def test_catalog_embed_does_not_call_the_model_for_a_full_list():
    class Fake:
        def __init__(self):
            self.called = False

        def missing(self, texts):
            return list(texts)

        def embed(self, texts):
            self.called = True

    fake = Fake()
    names = [f"позиция {index}" for index in range(EMBED_BATCH + 1)]
    with pytest.raises(CatalogEmbedRequired):
        embed_catalog(fake, names, allow_large=False)
    assert fake.called is False
    embed_catalog(fake, ["одна"], allow_large=False)
    assert fake.called is True


def test_embedder_reuses_cache_without_a_client(tmp_path, monkeypatch):
    import matching.config as config
    import matching.embeddings as embeddings

    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    vector = _unit(1, 0, 0)
    np.savez_compressed(tmp_path / "embeddings_bge_m3.npz", vectors=np.vstack([vector]))
    (tmp_path / "embeddings_meta.json").write_text(
        json.dumps({"model": config.EMBED_MODEL, "dim": 3, "texts": ["Перчатки х/б"]}),
        encoding="utf-8",
    )
    embedder = embeddings.Embedder()
    found = embedder.embed(["Перчатки х/б"])
    assert embedder.api_calls == 0
    assert embedder.missing(["Перчатки х/б"]) == []
    np.testing.assert_allclose(found["Перчатки х/б"], vector)
    embed_catalog(embedder, ["Перчатки х/б"], allow_large=False)
    assert embedder.api_calls == 0


def test_score_owner_labels_does_not_move_the_threshold():
    labels = [
        {"decision": "fit", "closeness": 0.9},
        {"decision": "fit", "closeness": 0.5},
        {"decision": "reject", "closeness": 0.95},
        {"decision": "reject", "closeness": 0.4},
    ]
    score = score_owner_labels(labels, threshold=0.74)
    assert score == {
        "human_fit": 2,
        "human_fit_above": 1,
        "human_reject": 2,
        "human_reject_below": 1,
    }
    assert FIT_MIN_COSINE == 0.74


def test_read_owner_labels_from_sqlite(tmp_path):
    path = tmp_path / "app.db"
    import sqlite3

    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE match_labels ("
        "procedure_id INTEGER, level TEXT, lines_json TEXT, decision TEXT, "
        "closeness REAL, telegram_user_id INTEGER, answered_at TEXT)"
    )
    connection.execute(
        "INSERT INTO match_labels VALUES (7, 'borderline', ?, 'reject', 0.61, 11, '2026-10-25')",
        (json.dumps([{"tender_name": "Мыло", "category": "мыло"}], ensure_ascii=False),),
    )
    connection.commit()
    connection.close()
    labels_found = read_owner_labels(path)
    assert labels_found[0]["procedure_id"] == 7
    assert labels_found[0]["decision"] == "reject"
    assert labels_found[0]["lines"][0]["category"] == "мыло"
    assert read_owner_labels(tmp_path / "missing.db") == []


@pytest.fixture
async def session():
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
    reset_engine()
    engine = create_async_engine_from_url(os.environ["DATABASE_URL"])
    bind_engine(engine)
    await init_db_schema(engine)
    maker = get_session_maker()
    async with maker() as db_session:
        yield db_session
        await db_session.commit()
    reset_engine()
    if prev is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = prev


@pytest.mark.asyncio
async def test_owner_feedback_is_stored(session):
    record = {
        "procedure_id": 15,
        "bucket": "borderline",
        "closeness": 0.61,
        "in_scope": 1,
        "line_count": 3,
        "lines": [
            {
                "relation": "in_scope",
                "tender_name": "Мыло",
                "category": "мыло",
                "embed_cosine": 0.61,
                "similar": ["Мыло хозяйственное 200г"],
                "qty": 4,
                "unit": "шт",
            },
            {"relation": "out_of_scope", "tender_name": "Труба"},
        ],
    }
    digest = "abc12345ff"
    await labels.save_offer(session, record, content_hash=digest)
    assert await labels.record_feedback(
        session,
        procedure_id=15,
        content_token="abc12345",
        decision="fit",
        telegram_user_id=9,
    )
    assert await labels.record_feedback(
        session,
        procedure_id=15,
        content_token="abc12345",
        decision="reject",
        telegram_user_id=9,
    )
    assert not await labels.record_feedback(
        session,
        procedure_id=15,
        content_token="ffffffff",
        decision="fit",
        telegram_user_id=9,
    )
    rows = (await session.scalars(select(MatchLabel))).all()
    assert len(rows) == 1
    assert rows[0].decision == "reject"
    assert rows[0].level == "borderline"
    assert rows[0].closeness == pytest.approx(0.61)
    stored = json.loads(rows[0].lines_json)
    assert stored == [
        {
            "tender_name": "Мыло",
            "category": "мыло",
            "embed_cosine": 0.61,
            "similar": ["Мыло хозяйственное 200г"],
            "qty": 4,
            "unit": "шт",
        }
    ]


def test_real_price_list_yields_product_kinds():
    from matching.catalog import find_price_xls, load_catalog

    try:
        path = find_price_xls()
    except FileNotFoundError:
        pytest.skip("price xls is not in the workspace")
    skus = load_catalog(path)
    assert len(skus) > 10_000
    categories = {sku.category for sku in skus}
    for name in (
        "перчатки",
        "пакеты и мешки",
        "посуда одноразовая",
        "бумажная продукция",
        "средства от насекомых",
    ):
        assert name in categories
    assert "НЭФИС" not in categories
    assert "Henkel" not in categories
    assert "Артпласт" not in categories
