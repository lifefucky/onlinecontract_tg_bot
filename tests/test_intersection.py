"""Hash, card text, shown-procedure ledger, and match-poll failure notices."""

import asyncio
import json
import logging
import os

import pytest

import jobs.intersection_feed as intersection_feed
from db.repositories import kv, shown, users
from db.schemas.user import UserStart
from db.session import bind_engine, create_async_engine_from_url, get_session_maker, init_db_schema, reset_engine
from jobs.intersection_feed import (
    SEEN_PROCEDURES_KEY,
    active_list_trusted,
    load_seen_procedure_ids,
    match_poll_interval_seconds,
    match_poll_loop,
    next_seen_ids,
    notify_match_admins,
    parse_procedure_ids,
    poll_new_matches,
    post_match_records,
    resolve_baseline,
    save_seen_procedure_ids,
    select_new_procedures,
    select_unseen,
)
from jobs.match_cards import content_hash, format_match_card


@pytest.fixture(autouse=True)
def _isolate_database_url():
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
    yield
    if prev is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = prev
    reset_engine()


@pytest.fixture(autouse=True)
async def _ready_memory_db(_isolate_database_url):
    """Poll tests look up started users before they open a session themselves."""
    reset_engine()
    engine = create_async_engine_from_url(os.environ["DATABASE_URL"])
    bind_engine(engine)
    await init_db_schema(engine)
    yield
    reset_engine()


@pytest.fixture
async def session():
    reset_engine()
    engine = create_async_engine_from_url(os.environ["DATABASE_URL"])
    bind_engine(engine)
    await init_db_schema(engine)
    maker = get_session_maker()
    async with maker() as db_session:
        yield db_session
        await db_session.commit()


def _record(**overrides):
    record = {
        "bucket": "fit",
        "procedure_id": 588845,
        "link": "https://onlinecontract.ru/tenders/588845",
        "owner": 'АО "ЗАВОД"',
        "name": "Перчатки <СИЗ>",
        "trade_window": {"start": "23.10.2026 10:00", "end": "23.10.2026 11:00"},
        "categories": ["перчатки", "пакеты"],
        "in_scope": 1,
        "line_count": 2,
        "closeness": 0.81,
        "lines": [
            {
                "tender_name": "Перчатки х/б",
                "qty": 12000,
                "unit": "Шт.",
                "relation": "in_scope",
                "category": "перчатки",
                "similar": ["Перчатки х/б 10 класс", "Перчатки х/б эконом"],
            },
            {"tender_name": "Микроволновка", "qty": 1, "unit": "шт", "relation": "out_of_scope"},
        ],
    }
    record.update(overrides)
    return record


def test_content_hash_ignores_out_of_catalog_and_changes_with_qty():
    left = content_hash(_record())
    right = content_hash(_record())
    assert left == right
    changed = _record()
    changed["lines"][0]["qty"] = 1
    assert content_hash(changed) != left
    extra_noise = _record()
    extra_noise["lines"].append(
        {"tender_name": "Холодильник", "qty": 2, "unit": "шт", "relation": "out_of_scope"}
    )
    assert content_hash(extra_noise) == left


def test_select_unseen_skips_known_procedure_even_when_lines_change():
    record = _record()
    known = {588845}
    assert select_unseen([record], known) == []
    changed = _record()
    changed["lines"][0]["qty"] = 1
    assert content_hash(changed) != content_hash(record)
    assert select_unseen([changed], known) == []
    fresh = _record(procedure_id=1, bucket="borderline", categories=["мыло"], in_scope=1, line_count=1)
    pending = select_unseen([record, fresh, {"bucket": "drop", "procedure_id": 9}], known)
    assert [item["procedure_id"] for item in pending] == [1]
    assert len(select_unseen([fresh, fresh], set())) == 1


def test_format_match_card_escapes_title_and_splits_audience():
    record = _record(
        owner='АО "Казанский жировой комбинат" <script>',
        link="https://onlinecontract.ru/tenders/588845?a=1&b=2",
        name="Хозяйственные товары <СИЗ>",
        useNDS=1,
        nds=20,
        ownerSrok="Постоплата не менее 60 дней",
        ownerSklad="Разовая закупка, апрель 2025.",
        confidence="high",
        possible_duplicate_of=544775,
    )
    user = format_match_card(record)
    admin = format_match_card(record, for_admin=True)
    assert '<a href="https://onlinecontract.ru/tenders/588845?a=1&amp;b=2">' in user
    assert "АО &quot;Казанский жировой комбинат&quot; &lt;script&gt;[588845]</a>" in user
    assert "<script>" not in user
    assert "a=1&b=2" not in user
    assert "Наименование:" in user
    assert "Хозяйственные товары &lt;СИЗ&gt;" in user
    assert "20 %" in user
    assert "Условия:" in user
    assert "Условия оплаты:" in user
    assert "Постоплата не менее 60 дней" in user
    assert "Сроки поставки:" in user
    assert "Разовая закупка, апрель 2025." in user
    assert "Подходит" not in user
    assert "Пограничный" not in user
    assert "Почему" not in user
    assert "Категории" not in user
    assert "похоже на" not in user
    assert "Похожий лист" not in user
    assert "Окно" not in user
    assert admin.startswith(user)
    assert "Подходит" in admin
    assert "Почему" not in admin
    assert "Категории" in admin
    assert "перчатки, пакеты" in admin
    assert "1 из 2" in admin
    assert "похоже на" in admin
    assert "Перчатки х/б 10 класс" in admin
    assert "Перчатки х/б эконом" in admin
    assert "Похожий лист" in admin
    assert "544775" in admin
    assert "Микроволновка" not in admin
    assert "Уверенность" not in admin
    assert "high" not in admin

    borderline = _record(bucket="borderline", categories=["мыло"], in_scope=2, line_count=4, closeness=0.6)
    borderline_user = format_match_card(borderline)
    borderline_admin = format_match_card(borderline, for_admin=True)
    assert "Пограничный" not in borderline_user
    assert "Подходит" not in borderline_user
    assert "Почему" not in borderline_user
    assert "Пограничный" in borderline_admin
    assert "2 из 4" in borderline_admin
    assert "Почему" not in borderline_admin


def test_format_match_card_omits_missing_fields():
    bare = format_match_card(_record(name="  "))
    assert "Наименование" not in bare
    assert "НДС" not in bare
    assert "Условия" not in bare
    without_vat = format_match_card(_record(useNDS=0, nds=20, ownerSrok="  ", ownerSklad=""))
    assert "НДС:</b> Нет" in without_vat
    assert "20 %" not in without_vat
    assert "Условия" not in without_vat
    payment_only = format_match_card(
        _record(useNDS=True, nds=None, ownerSrok="Постоплата", ownerSklad=None)
    )
    assert "НДС" not in payment_only
    assert "Условия оплаты:" in payment_only
    assert "Сроки поставки" not in payment_only


def test_public_record_carries_vat_and_shared_terms(tmp_path, monkeypatch):
    import matching.match as match
    import matching.tenders as tenders

    cache = tmp_path / "cache"
    folder = cache / "positions"
    folder.mkdir(parents=True)
    monkeypatch.setattr(tenders, "CACHE_DIR", cache)
    same = {
        "data": [
            {
                "name": "Мыло",
                "totalCount": 1,
                "unit": "шт",
                "price": 10,
                "ownerSrok": " Постоплата не менее 60 дней ",
                "ownerSklad": "Разовая закупка",
            },
            {
                "name": "",
                "ownerSrok": "Постоплата не менее 60 дней",
                "ownerSklad": "Разовая закупка",
            },
        ]
    }
    mixed = {
        "data": [
            {"name": "А", "ownerSrok": "один", "ownerSklad": "апрель"},
            {"name": "Б", "ownerSrok": "два", "ownerSklad": "апрель"},
        ]
    }
    (folder / "5.json").write_text(json.dumps(same), encoding="utf-8")
    (folder / "6.json").write_text(json.dumps(mixed), encoding="utf-8")
    _positions, terms = tenders.fetch_positions([5, 6])
    assert terms[5] == {
        "ownerSrok": "Постоплата не менее 60 дней",
        "ownerSklad": "Разовая закупка",
    }
    assert terms[6] == {"ownerSklad": "апрель"}
    procedure = {
        "id": 5,
        "owner": {"name": 'АО "Тест"'},
        "name": "Хозяйственные товары",
        "useNDS": 1,
        "nds": 20,
        **terms[5],
    }
    lines = [{"relation": "in_scope", "tender_name": "Мыло", "category": "мыло", "embed_cosine": 0.8}]
    record = match._public_record(procedure, lines, "fit", 0.8)
    assert record["useNDS"] == 1
    assert record["nds"] == 20
    assert record["owner"] == 'АО "Тест"'
    assert record["ownerSrok"] == "Постоплата не менее 60 дней"
    assert record["ownerSklad"] == "Разовая закупка"
    assert "snapshot_date" not in record
    assert "shown_procedures" in record["show_ledger"]
    assert "bot_message_tracks" in record["show_ledger"]
    assert "не хранится" not in record["show_ledger"]
    no_vat = {"id": 6, "name": "x", **terms[6]}
    bare = match._public_record(no_vat, lines, "fit", 0.8)
    assert "useNDS" not in bare
    assert "nds" not in bare
    assert "ownerSrok" not in bare
    assert bare["ownerSklad"] == "апрель"


def test_select_new_procedures_skips_seen_and_duplicates():
    rows = [{"id": 1}, {"id": 2}, {"id": 2}, {"id": "x"}, {"id": 3}, {"id": True}]
    assert [row["id"] for row in select_new_procedures(rows, {2})] == [1, 3]


def test_resolve_baseline_prefers_stored_snapshot():
    seen, persist = resolve_baseline(None, {1, 2})
    assert seen == {1, 2} and persist
    seen, persist = resolve_baseline({1}, {1, 2, 9})
    assert seen == {1} and not persist
    seen, persist = resolve_baseline(set(), {5, 6})
    assert seen == set() and not persist
    seen, persist = resolve_baseline(None, None)
    assert seen == set() and persist


def test_active_list_trust_rejects_truncated_page():
    previous = set(range(500))
    assert active_list_trusted(previous, set(range(480, 980)))
    assert not active_list_trusted(previous, set(range(100)))
    assert not active_list_trusted(previous, set())
    assert active_list_trusted(set(), {1, 2, 3})


def test_next_seen_ids_keeps_failures_for_retry():
    assert next_seen_ids({1, 2, 3}, {2}) == {1, 3}


def test_parse_procedure_ids_rejects_bool():
    with pytest.raises(ValueError):
        parse_procedure_ids([True])


def test_match_poll_interval(monkeypatch):
    monkeypatch.setenv("MATCH_POLL_INTERVAL_MINUTES", "")
    assert match_poll_interval_seconds() == 15 * 60
    monkeypatch.setenv("MATCH_POLL_INTERVAL_MINUTES", "10")
    assert match_poll_interval_seconds() == 600
    monkeypatch.setenv("MATCH_POLL_INTERVAL_MINUTES", "0")
    assert match_poll_interval_seconds() == 15 * 60
    monkeypatch.setenv("MATCH_POLL_INTERVAL_MINUTES", "нет")
    assert match_poll_interval_seconds() == 15 * 60


@pytest.mark.asyncio
async def test_shown_ledger_remembers_procedure(session):
    assert await shown.known_keys(session, 7) == set()
    await shown.mark_shown(
        session,
        telegram_chat_id=7,
        procedure_id=588845,
        content_hash="abc",
        bucket="exact",
    )
    await shown.mark_shown(
        session,
        telegram_chat_id=7,
        procedure_id=588845,
        content_hash="def",
        bucket="exact",
    )
    known = await shown.known_keys(session, 7)
    assert known == {588845}
    assert await shown.known_keys(session, 8) == set()
    changed = _record()
    changed["lines"][0]["qty"] = 3
    assert select_unseen([changed], known) == []


@pytest.mark.asyncio
async def test_seen_snapshot_roundtrip(session):
    assert await load_seen_procedure_ids(session) is None
    await save_seen_procedure_ids(session, {3, 1})
    assert await load_seen_procedure_ids(session) == {1, 3}


@pytest.mark.asyncio
async def test_seen_snapshot_rejects_garbage(session):
    await kv.set_json(session, SEEN_PROCEDURES_KEY, {"nope": 1})
    with pytest.raises(ValueError):
        await load_seen_procedure_ids(session)


class RecordingBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.markups: list[object] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((int(chat_id), text))
        self.markups.append(reply_markup)


def _notices(bot: RecordingBot) -> list[str]:
    return [text for _chat, text in bot.sent if text.startswith("⚠️")]


def _assert_notice_is_safe(text: str) -> None:
    lowered = text.lower()
    assert "secret" not in lowered
    assert "token" not in lowered
    assert "openai" not in lowered
    assert "sk-" not in lowered
    assert "traceback" not in lowered


def _patch_poll(monkeypatch, *, stored, load, match=None):
    async def read_seen():
        if isinstance(stored, Exception):
            raise stored
        return stored

    saved: list[set[int]] = []

    async def write_seen(ids):
        saved.append(set(ids))

    async def read_state():
        return "same", "same"

    def live_state():
        return "same", "same"

    async def write_state(_fingerprint, _rules):
        return None

    async def warn(_bot):
        return None

    monkeypatch.setattr(intersection_feed, "_read_seen", read_seen)
    monkeypatch.setattr(intersection_feed, "_load_new_procedures", load)
    monkeypatch.setattr(intersection_feed, "_write_seen", write_seen)
    monkeypatch.setattr(intersection_feed, "_read_match_state", read_state)
    monkeypatch.setattr(intersection_feed, "_live_match_state", live_state)
    monkeypatch.setattr(intersection_feed, "_write_match_state", write_state)
    monkeypatch.setattr(intersection_feed, "_warn_stale_price", warn)
    if match is not None:
        monkeypatch.setattr(intersection_feed, "_match_procedures", match)
    return saved


def _pipeline_error(message: str):
    from matching.errors import PipelineError

    return PipelineError(message)


async def _prepare_memory_db():
    reset_engine()
    engine = create_async_engine_from_url(os.environ["DATABASE_URL"])
    bind_engine(engine)
    await init_db_schema(engine)
    return get_session_maker()


@pytest.mark.asyncio
async def test_borderline_goes_only_to_owners(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "11")
    monkeypatch.setattr(intersection_feed, "SEND_GAP_SECONDS", 0)
    maker = await _prepare_memory_db()
    async with maker() as session:
        await users.register_start(session, UserStart(telegram_user_id=400, first_name="Глеб"))
        await session.commit()
    bot = RecordingBot()
    sent, failed = await post_match_records(bot, [_record(bucket="borderline")])
    assert sent == 1
    assert failed == set()
    assert [chat for chat, _text in bot.sent] == [11, 11]
    assert "Пограничный" in bot.sent[1][1]


@pytest.mark.asyncio
async def test_poll_rematches_all_when_the_price_fingerprint_changes(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "10")

    def load(_seen):
        raise AssertionError("new-only path")

    def load_all():
        return [{"id": 1}, {"id": 2}], {1, 2}

    seen_calls = []

    def match(procedures):
        seen_calls.append([row["id"] for row in procedures])
        return [], [], set()

    saved = _patch_poll(monkeypatch, stored={1, 2}, load=load, match=match)

    async def read_state():
        return "old-file", "rules"

    def live_state():
        return "new-file", "rules"

    monkeypatch.setattr(intersection_feed, "_read_match_state", read_state)
    monkeypatch.setattr(intersection_feed, "_live_match_state", live_state)
    monkeypatch.setattr(intersection_feed, "_load_all_procedures", load_all)
    bot = RecordingBot()
    assert await poll_new_matches(bot) == 0
    assert seen_calls == [[1, 2]]
    assert saved == [{1, 2}]
    assert bot.sent == []


@pytest.mark.asyncio
async def test_missing_catalog_embeddings_warn_once_per_day(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "10")
    await _prepare_memory_db()

    def load(_seen):
        return [{"id": 9}], {1, 9}

    def match(_procedures):
        from matching.errors import CatalogEmbedRequired

        raise CatalogEmbedRequired("not sent")

    _patch_poll(monkeypatch, stored={1}, load=load, match=match)
    first = RecordingBot()
    assert await poll_new_matches(first) == 0
    assert len(_notices(first)) == 1
    assert "эмбеддинг" in _notices(first)[0].lower()
    _assert_notice_is_safe(_notices(first)[0])
    second = RecordingBot()
    assert await poll_new_matches(second) == 0
    assert second.sent == []


@pytest.mark.asyncio
async def test_post_skips_procedure_already_shown(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "10,11")
    monkeypatch.setattr(intersection_feed, "SEND_GAP_SECONDS", 0)
    maker = await _prepare_memory_db()
    async with maker() as session:
        await shown.mark_shown(
            session,
            telegram_chat_id=10,
            procedure_id=588845,
            content_hash="old-hash",
            bucket="exact",
        )
        await session.commit()

    changed = _record()
    changed["lines"][0]["qty"] = 7
    bot = RecordingBot()
    sent, failed = await post_match_records(bot, [changed])
    assert sent == 1
    assert failed == set()
    assert _notices(bot) == []
    assert [text for chat, text in bot.sent if chat == 10] == []
    assert any("588845" in text for chat, text in bot.sent if chat == 11)

    changed["lines"][0]["qty"] = 8
    again = RecordingBot()
    sent_again, failed_again = await post_match_records(again, [changed])
    assert sent_again == 0
    assert failed_again == set()
    assert again.sent == []


def _callbacks(bot: RecordingBot, chat_id: int) -> list[str]:
    found: list[str] = []
    for (chat, _text), markup in zip(bot.sent, bot.markups):
        if chat != chat_id or markup is None:
            continue
        found.append(markup.inline_keyboard[0][0].callback_data)
    return found


@pytest.mark.asyncio
async def test_posts_go_to_started_users_and_spare_admins(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "11,20")
    monkeypatch.setattr(intersection_feed, "SEND_GAP_SECONDS", 0)
    maker = await _prepare_memory_db()
    record = _record(
        useNDS=True,
        nds=20,
        ownerSrok="Постоплата не менее 60 дней",
        ownerSklad="Разовая закупка, апрель 2025.",
        confidence="high",
    )
    async with maker() as session:
        await users.register_start(session, UserStart(telegram_user_id=100, first_name="Анна"))
        await users.register_start(session, UserStart(telegram_user_id=200, first_name="Борис"))
        await users.register_start(session, UserStart(telegram_user_id=300, first_name="Вера"))
        await users.register_start(session, UserStart(telegram_user_id=400, first_name="Глеб"))
        await users.ban_user(session, 200)
        await users.mark_bot_blocked(session, 300, blocked=True)
        await users.ban_user(session, 20)
        await shown.mark_shown(
            session,
            telegram_chat_id=100,
            procedure_id=588845,
            content_hash="old",
            bucket="exact",
        )
        await session.commit()

    bot = RecordingBot()
    sent, failed = await post_match_records(bot, [record])
    assert sent == 2
    assert failed == set()
    assert [chat for chat, _text in bot.sent] == [11, 11, 400]
    header, admin_card = [text for chat, text in bot.sent if chat == 11]
    user_card = bot.sent[-1][1]
    assert header == "Пересечения с прайсом: подходит 1, пограничных 0."
    assert admin_card == format_match_card(record, for_admin=True)
    assert user_card == format_match_card(record, for_admin=False)
    assert "Подходит" in admin_card
    assert "Категории" in admin_card
    assert "Почему" not in admin_card
    assert "Подходит" not in user_card
    assert "Пограничный" not in user_card
    assert "Почему" not in user_card
    assert "Пересечения с прайсом" not in user_card
    assert _callbacks(bot, 11) == ["pos_588845"]
    assert _callbacks(bot, 400) == ["pos_588845"]
    admin_markups = [markup for (chat, _text), markup in zip(bot.sent, bot.markups) if chat == 11 and markup]
    user_markups = [markup for (chat, _text), markup in zip(bot.sent, bot.markups) if chat == 400 and markup]
    assert len(admin_markups) == 1 and len(admin_markups[0].inline_keyboard) == 2
    assert [button.text for button in admin_markups[0].inline_keyboard[1]] == ["Подходит", "Не подходит"]
    assert admin_markups[0].inline_keyboard[1][0].callback_data.startswith("mf|588845|1|")
    assert admin_markups[0].inline_keyboard[1][1].callback_data.startswith("mf|588845|0|")
    assert len(user_markups) == 1 and len(user_markups[0].inline_keyboard) == 1
    assert _notices(bot) == []


@pytest.mark.asyncio
async def test_header_failure_notifies_once_and_does_not_mark(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "10")
    monkeypatch.setattr(intersection_feed, "SEND_GAP_SECONDS", 0)
    maker = await _prepare_memory_db()

    class FailHeader(RecordingBot):
        async def send_message(self, chat_id, text, reply_markup=None):
            self.sent.append((int(chat_id), text))
            if not text.startswith("⚠️"):
                raise RuntimeError("telegram token=SECRET")

    bot = FailHeader()
    sent, failed = await post_match_records(bot, [_record(), _record(procedure_id=3)])
    assert sent == 0
    assert failed == {588845, 3}
    notices = _notices(bot)
    assert len(notices) == 1
    assert "588845" in notices[0] and "3" in notices[0]
    _assert_notice_is_safe(notices[0])
    assert len(bot.sent) == 2
    async with maker() as session:
        assert await shown.known_keys(session, 10) == set()


@pytest.mark.asyncio
async def test_card_failures_are_one_notice(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "10")
    monkeypatch.setattr(intersection_feed, "SEND_GAP_SECONDS", 0)
    await _prepare_memory_db()

    class FailCards(RecordingBot):
        async def send_message(self, chat_id, text, reply_markup=None):
            self.sent.append((int(chat_id), text))
            if text.startswith("Пересечения") or text.startswith("⚠️"):
                return
            raise RuntimeError("card token=SECRET")

    bot = FailCards()
    sent, failed = await post_match_records(bot, [_record(), _record(procedure_id=3)])
    assert sent == 0
    assert failed == {588845, 3}
    notices = _notices(bot)
    assert len(notices) == 1
    assert "588845" in notices[0] and "3" in notices[0]
    _assert_notice_is_safe(notices[0])


@pytest.mark.asyncio
async def test_poll_without_new_procedures_stays_quiet(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "10")

    def load(seen):
        return [], {1, 2, 3}

    def match(_procedures):
        raise AssertionError("classifier ran")

    saved = _patch_poll(monkeypatch, stored={1, 2, 3}, load=load, match=match)
    bot = RecordingBot()
    assert await poll_new_matches(bot) == 0
    assert bot.sent == []
    assert saved == [{1, 2, 3}]


@pytest.mark.asyncio
async def test_poll_with_no_intersections_stays_quiet(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "10")

    def load(seen):
        return [{"id": 9}], {1, 9}

    def match(_procedures):
        return [], [], set()

    saved = _patch_poll(monkeypatch, stored={1}, load=load, match=match)
    bot = RecordingBot()
    assert await poll_new_matches(bot) == 0
    assert bot.sent == []
    assert saved == [{1, 9}]


@pytest.mark.asyncio
async def test_poll_failure_notice_skips_started_users(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "10,20")
    maker = await _prepare_memory_db()
    async with maker() as session:
        await users.register_start(session, UserStart(telegram_user_id=100, first_name="Анна"))
        await session.commit()

    def load(_seen):
        raise RuntimeError("connection reset token=SECRET")

    _patch_poll(monkeypatch, stored={1, 2}, load=load)
    bot = RecordingBot()
    assert await poll_new_matches(bot) == 0
    assert [chat for chat, _text in bot.sent] == [10, 20]
    notice = bot.sent[0][1]
    assert notice.startswith("⚠️")
    assert "список конкурсных листов" in notice.lower()
    _assert_notice_is_safe(notice)


@pytest.mark.asyncio
async def test_poll_notifies_when_procedure_list_fails(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "10,20")

    def load(_seen):
        raise RuntimeError("connection reset token=SECRET")

    saved = _patch_poll(monkeypatch, stored={1, 2}, load=load)
    bot = RecordingBot()
    assert await poll_new_matches(bot) == 0
    assert saved == []
    assert [chat for chat, _text in bot.sent] == [10, 20]
    assert len({text for _chat, text in bot.sent}) == 1
    notice = bot.sent[0][1]
    assert notice.startswith("⚠️")
    assert "список конкурсных листов" in notice.lower()
    _assert_notice_is_safe(notice)


@pytest.mark.asyncio
async def test_poll_notifies_when_procedure_page_is_truncated(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "10")

    def load(_seen):
        raise RuntimeError("procedure list truncated at offset 200 of 900 key=sk-test-secret")

    _patch_poll(monkeypatch, stored={1}, load=load)
    bot = RecordingBot()
    assert await poll_new_matches(bot) == 0
    assert len(_notices(bot)) == 1
    notice = _notices(bot)[0]
    assert "неполной странице" in notice
    _assert_notice_is_safe(notice)


@pytest.mark.asyncio
async def test_poll_notifies_when_active_list_looks_truncated(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "15,16")

    def load(_seen):
        return [], set(range(100))

    def match(_procedures):
        raise AssertionError("classifier ran")

    saved = _patch_poll(monkeypatch, stored=set(range(500)), load=load, match=match)
    bot = RecordingBot()
    assert await poll_new_matches(bot) == 0
    assert saved == []
    assert [chat for chat, _text in bot.sent] == [15, 16]
    notice = bot.sent[0][1]
    assert "обрезан" in notice
    assert "сейчас 100" in notice
    assert "было 500" in notice
    assert notice == bot.sent[1][1]


@pytest.mark.asyncio
async def test_poll_notifies_when_matching_fails(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "10")

    def load(_seen):
        return [{"id": 9}], {1, 9}

    def match(_procedures):
        raise _pipeline_error("OPENAI_API_KEY is missing sk-test-secret")

    saved = _patch_poll(monkeypatch, stored={1}, load=load, match=match)
    bot = RecordingBot()
    assert await poll_new_matches(bot) == 0
    assert saved == []
    notice = _notices(bot)[0]
    assert "сопоставить" in notice
    assert "9" in notice
    _assert_notice_is_safe(notice)


@pytest.mark.asyncio
async def test_poll_notifies_when_matching_exits(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "10")

    def load(_seen):
        return [{"id": 4}], {1, 4}

    def match(_procedures):
        raise SystemExit("secret exit")

    _patch_poll(monkeypatch, stored={1}, load=load, match=match)
    bot = RecordingBot()
    assert await poll_new_matches(bot) == 0
    assert len(_notices(bot)) == 1
    assert "4" in _notices(bot)[0]
    _assert_notice_is_safe(_notices(bot)[0])


@pytest.mark.asyncio
async def test_poll_notifies_once_for_position_errors(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "15,16")

    def load(_seen):
        return [{"id": 4}, {"id": 5}, {"id": 6}], {1, 4, 5, 6}

    def match(_procedures):
        return [], [], {4, 5, 6}

    saved = _patch_poll(monkeypatch, stored={1}, load=load, match=match)
    bot = RecordingBot()
    assert await poll_new_matches(bot) == 0
    assert saved == [{1}]
    assert [chat for chat, _text in bot.sent] == [15, 16]
    assert len({text for _chat, text in bot.sent}) == 1
    notice = bot.sent[0][1]
    assert "4, 5, 6" in notice
    assert notice.startswith("⚠️")


@pytest.mark.asyncio
async def test_post_failure_stays_out_of_snapshot(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "10")

    def load(_seen):
        return [{"id": 9}], {1, 9}

    def match(_procedures):
        return [_record(procedure_id=9)], [], set()

    async def post(_bot, records):
        assert [record["procedure_id"] for record in records] == [9]
        return 0, {9}

    saved = _patch_poll(monkeypatch, stored={1}, load=load, match=match)
    monkeypatch.setattr(intersection_feed, "post_match_records", post)
    bot = RecordingBot()
    assert await poll_new_matches(bot) == 0
    assert bot.sent == []
    assert saved == [{1}]


@pytest.mark.asyncio
async def test_poll_notifies_when_snapshot_is_unreadable(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "10")

    def load(_seen):
        raise AssertionError("list was fetched")

    saved = _patch_poll(
        monkeypatch,
        stored=ValueError("garbage token=SECRET"),
        load=load,
    )
    bot = RecordingBot()
    assert await poll_new_matches(bot) == 0
    assert saved == []
    assert len(_notices(bot)) == 1
    notice = _notices(bot)[0]
    assert "не читается" in notice
    _assert_notice_is_safe(notice)


@pytest.mark.asyncio
async def test_match_poll_loop_notifies_on_unexpected_error(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "42")
    monkeypatch.setattr(intersection_feed, "match_poll_interval_seconds", lambda: 0)
    calls = {"n": 0}

    async def poll(_bot):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom token=SECRET")
        raise asyncio.CancelledError()

    monkeypatch.setattr(intersection_feed, "poll_new_matches", poll)
    bot = RecordingBot()
    with pytest.raises(asyncio.CancelledError):
        await match_poll_loop(bot)
    assert len(_notices(bot)) == 1
    assert "завершилась с ошибкой" in _notices(bot)[0]
    _assert_notice_is_safe(_notices(bot)[0])
    assert bot.sent[0][0] == 42


@pytest.mark.asyncio
async def test_match_poll_loop_stays_quiet_on_cancel(monkeypatch):
    monkeypatch.setattr(intersection_feed, "match_poll_interval_seconds", lambda: 0)

    async def poll(_bot):
        raise asyncio.CancelledError()

    monkeypatch.setattr(intersection_feed, "poll_new_matches", poll)
    bot = RecordingBot()
    with pytest.raises(asyncio.CancelledError):
        await match_poll_loop(bot)
    assert bot.sent == []


@pytest.mark.asyncio
async def test_admin_notice_failure_is_not_retried(monkeypatch, caplog):
    monkeypatch.setenv("ADMIN_IDS", "1,2")

    class BadBot:
        def __init__(self):
            self.calls = 0

        async def send_message(self, chat_id, text, reply_markup=None):
            self.calls += 1
            raise RuntimeError("blocked")

    bot = BadBot()
    with caplog.at_level(logging.ERROR, logger="jobs.intersection_feed"):
        await notify_match_admins(bot, "Не удалось получить список конкурсных листов.")
    assert bot.calls == 2
    assert caplog.text.count("admin notice failed") == 2

    monkeypatch.setenv("ADMIN_IDS", "nope")
    bot.calls = 0
    with caplog.at_level(logging.ERROR, logger="jobs.intersection_feed"):
        await notify_match_admins(bot, "Не удалось получить список конкурсных листов.")
    assert bot.calls == 0
    assert "ADMIN_IDS is unreadable" in caplog.text
