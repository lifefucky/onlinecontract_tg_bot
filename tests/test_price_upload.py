"""Shared price upload: reject a bad workbook, confirm replaces one file, cancel does not."""

import os
import struct
from types import SimpleNamespace

import pytest

import matching.config as config
import tgbot.admin as admin
from db.repositories import kv
from db.session import bind_engine, create_async_engine_from_url, get_session_maker, init_db_schema, reset_engine
from jobs.embed_lock import catalog_embed_lock
from matching.catalog import PRICE_PENDING_KEY, PRICE_UPLOAD_KEY


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


@pytest.fixture
def price_dir(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "RESEARCH_DIR", tmp_path / "no-research")
    monkeypatch.delenv("PRICE_LIST_PATH", raising=False)
    monkeypatch.setattr(admin, "get_admin_ids", lambda: [7])
    return data


def _xls_bytes(rows: list[tuple[str, float]]) -> bytes:
    """Minimal Excel 2 worksheet. Names must be latin-1 and shorter than 256 bytes."""

    def rec(op: int, data: bytes = b"") -> bytes:
        return struct.pack("<HH", op, len(data)) + data

    parts = [rec(0x0009, struct.pack("<HH", 0x0200, 0x0010))]
    for index, (name, price) in enumerate(rows):
        raw = name.encode("latin1")
        label = struct.pack("<HH", index, 0) + b"\x00\x00\x00" + bytes([len(raw)]) + raw
        number = struct.pack("<HH", index, 1) + b"\x00\x00\x00" + struct.pack("<d", float(price))
        parts.append(rec(0x0004, label))
        parts.append(rec(0x0003, number))
    parts.append(rec(0x000A))
    return b"".join(parts)


_ONE = _xls_bytes([("Gloves cotton 10", 12.5)])
_FOUR = _xls_bytes(
    [
        ("Gloves cotton 10", 12.5),
        ("Soap liquid 500", 40.0),
        ("Trash bags 60l", 15.0),
        ("Paper towels 2ct", 30.0),
    ]
)


class FakeBot:
    def __init__(self, payload: bytes = b"", fail_chats: set[int] | None = None):
        self.payload = payload
        self.downloads = 0
        self.sent: list[tuple[int, str]] = []
        self.fail_chats = fail_chats or set()

    async def download(self, file, destination=None, timeout=30, chunk_size=65536, seek=True):
        self.downloads += 1
        destination.write_bytes(self.payload)

    async def send_message(self, chat_id, text, reply_markup=None):
        if int(chat_id) in self.fail_chats:
            raise RuntimeError("notify failed token=SECRET")
        self.sent.append((int(chat_id), text))


class FakeMessage:
    def __init__(self, bot: FakeBot, document, user_id: int = 7):
        self.bot = bot
        self.document = document
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[tuple[str, object]] = []

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


class FakeEditable:
    def __init__(self):
        self.edits: list[tuple[str, object]] = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))


class FakeQuery:
    def __init__(self, data: str, bot: FakeBot, message: FakeEditable, user_id: int = 7):
        self.data = data
        self.bot = bot
        self.message = message
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[tuple[object, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _document(name, size):
    return SimpleNamespace(file_name=name, file_size=size, file_id="doc")


def test_price_prompt_escapes_the_filename_and_warns_on_a_sharp_drop():
    text = admin.format_price_prompt("<b>price.xls", 1, 4)
    assert "&lt;b&gt;price.xls" in text
    assert "<b>price.xls" not in text
    assert "сильно меньше" in text
    assert "сильно меньше" not in admin.format_price_prompt("price.xls", 5, 10)
    assert "Сейчас в прайсе" not in admin.format_price_prompt("price.xls", 3, None)


async def _send(session, bot: FakeBot, name: str, size: int | None):
    message = FakeMessage(bot, _document(name, size))
    await admin.cmd_price_document(message, session, bot)
    return message


async def test_bare_price_asks_for_a_document():
    message = FakeMessage(FakeBot(), document=None)
    await admin.cmd_price(message)
    assert message.answers == [("Пришлите документ .xls и укажите в подписи команду /price.", None)]


async def test_reject_non_xls_and_oversize_before_download(session, price_dir):
    dest = price_dir / "price-list.xls"
    dest.write_bytes(b"keep")
    bot = FakeBot(payload=b"unused")
    message = await _send(session, bot, "price.xlsx", 20)
    assert bot.downloads == 0
    assert "xlsx" in message.answers[0][0]
    message = await _send(session, bot, None, 20)
    assert bot.downloads == 0
    message = await _send(session, bot, "price.xls", None)
    assert "размер" in message.answers[-1][0]
    message = await _send(session, bot, "price.xls", admin.MAX_PRICE_UPLOAD_BYTES + 1)
    assert "20" in message.answers[-1][0]
    assert bot.downloads == 0
    assert dest.read_bytes() == b"keep"
    assert not (price_dir / "price-list.pending.xls").exists()


async def test_reject_unreadable_and_empty_without_replacing(session, price_dir):
    dest = price_dir / "price-list.xls"
    dest.write_bytes(b"keep")
    garbage = FakeBot(payload=b"this is not a workbook")
    message = await _send(session, garbage, "bad.xls", len(garbage.payload))
    assert "не изменён" in message.answers[-1][0]
    assert dest.read_bytes() == b"keep"
    assert not (price_dir / "price-list.pending.xls").exists()
    assert await kv.get_json(session, PRICE_PENDING_KEY) is None

    empty = FakeBot(payload=_xls_bytes([]))
    message = await _send(session, empty, "empty.xls", len(empty.payload))
    assert "не изменён" in message.answers[-1][0]
    assert dest.read_bytes() == b"keep"
    assert not (price_dir / "price-list.pending.xls").exists()


async def test_confirm_writes_destination_and_rollback_copy(session, price_dir, monkeypatch):
    monkeypatch.setattr(admin, "count_missing_catalog_names", lambda: 3)
    monkeypatch.setattr(admin, "get_admin_ids", lambda: [7, 8])
    dest = price_dir / "price-list.xls"
    dest.write_bytes(_FOUR)
    bot = FakeBot(payload=_ONE)
    message = await _send(session, bot, "Прайс 26.04.2026.xls", len(_ONE))
    text, markup = message.answers[-1]
    assert "Прайс 26.04.2026.xls" in text
    assert "Позиций в новом файле: 1." in text
    assert "Сейчас в прайсе: 4." in text
    assert "сильно меньше" in text
    token = markup.inline_keyboard[0][1].callback_data
    assert token == f"pp|{markup.inline_keyboard[0][0].callback_data.split('|')[1]}|x"
    replace = markup.inline_keyboard[0][0].callback_data
    assert replace.startswith("pp|") and replace.endswith("|ok")
    assert len(replace.encode()) < 64

    edited = FakeEditable()
    query = FakeQuery(replace, bot, edited)
    await admin.on_price_upload(query, session, bot)
    assert dest.read_bytes() == _ONE
    assert (price_dir / "price-list.prev.xls").read_bytes() == _FOUR
    assert not (price_dir / "price-list.pending.xls").exists()
    assert await kv.get_json(session, PRICE_PENDING_KEY) is None
    stored = await kv.get_json(session, PRICE_UPLOAD_KEY)
    assert stored["original_name"] == "Прайс 26.04.2026.xls"
    assert stored["telegram_user_id"] == 7
    assert stored["path"] == str(dest)
    assert stored["uploaded_at"].endswith("+00:00")
    assert "В кэше нет эмбеддингов для 3 названий." in edited.edits[-1][0]
    assert edited.edits[-1][1].inline_keyboard[0][0].callback_data == "pe|1"
    assert bot.sent == [(8, "Прайс заменён. Позиций: 1. Загрузил id 7.")]


async def test_cancel_and_stale_token_leave_the_live_file(session, price_dir):
    dest = price_dir / "price-list.xls"
    dest.write_bytes(_FOUR)
    first = FakeBot(payload=_ONE)
    message = await _send(session, first, "first.xls", len(_ONE))
    old_token = message.answers[-1][1].inline_keyboard[0][0].callback_data.split("|")[1]
    second = FakeBot(payload=_FOUR)
    await _send(session, second, "second.xls", len(_FOUR))
    stale = FakeQuery(f"pp|{old_token}|ok", first, FakeEditable())
    await admin.on_price_upload(stale, session, first)
    assert stale.answers[-1][0] == "Эта загрузка уже неактуальна."
    assert dest.read_bytes() == _FOUR
    assert (price_dir / "price-list.pending.xls").read_bytes() == _FOUR

    current = await kv.get_json(session, PRICE_PENDING_KEY)
    cancel = FakeQuery(f"pp|{current['token']}|x", second, FakeEditable())
    await admin.on_price_upload(cancel, session, second)
    assert dest.read_bytes() == _FOUR
    assert not (price_dir / "price-list.pending.xls").exists()
    assert await kv.get_json(session, PRICE_PENDING_KEY) is None
    assert "отменена" in cancel.message.edits[-1][0]


async def test_confirm_uses_price_list_path_and_does_not_write_canonical(session, price_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "count_missing_catalog_names", lambda: 0)
    target = tmp_path / "custom" / "list.xls"
    target.parent.mkdir()
    target.write_bytes(_ONE)
    monkeypatch.setenv("PRICE_LIST_PATH", "custom/list.xls")
    bot = FakeBot(payload=_FOUR)
    message = await _send(session, bot, "list.xls", len(_FOUR))
    replace = message.answers[-1][1].inline_keyboard[0][0].callback_data
    edited = FakeEditable()
    await admin.on_price_upload(FakeQuery(replace, bot, edited), session, bot)
    assert target.read_bytes() == _FOUR
    assert (price_dir / "price-list.prev.xls").read_bytes() == _ONE
    assert not (price_dir / "price-list.xls").exists()
    assert "уже есть в кэше" in edited.edits[-1][0]
    assert edited.edits[-1][1] is None
    stored = await kv.get_json(session, PRICE_UPLOAD_KEY)
    assert stored["path"] == str(target)


async def test_notify_failure_does_not_roll_back_the_file(session, price_dir, monkeypatch):
    monkeypatch.setattr(admin, "count_missing_catalog_names", lambda: 0)
    monkeypatch.setattr(admin, "get_admin_ids", lambda: [7, 9])
    dest = price_dir / "price-list.xls"
    dest.write_bytes(_ONE)
    bot = FakeBot(payload=_FOUR, fail_chats={9})
    message = await _send(session, bot, "next.xls", len(_FOUR))
    replace = message.answers[-1][1].inline_keyboard[0][0].callback_data
    await admin.on_price_upload(FakeQuery(replace, bot, FakeEditable()), session, bot)
    assert dest.read_bytes() == _FOUR
    stored = await kv.get_json(session, PRICE_UPLOAD_KEY)
    assert stored["original_name"] == "next.xls"
    assert bot.sent == []


async def test_embed_button_reports_new_texts_and_a_busy_lock(monkeypatch):
    calls = {"n": 0}

    def embed():
        calls["n"] += 1
        calls["locked"] = catalog_embed_lock.locked()
        return 4

    monkeypatch.setattr(admin, "embed_live_catalog", embed)
    bot = FakeBot()
    edited = FakeEditable()
    await admin.on_price_embed(FakeQuery("pe|1", bot, edited), bot)
    assert calls["n"] == 1
    assert calls["locked"] is True
    assert edited.edits[-1][0] == "Посчитано новых названий: 4."
    assert admin._embed_running is False
    assert catalog_embed_lock.locked() is False

    await catalog_embed_lock.acquire()
    try:
        busy = FakeEditable()
        query = FakeQuery("pe|1", bot, busy)
        await admin.on_price_embed(query, bot)
        assert calls["n"] == 1
        assert query.answers[-1] == ("Расчёт уже выполняется.", True)
        assert busy.edits == []
    finally:
        catalog_embed_lock.release()


async def test_embed_failure_hides_the_exception(monkeypatch):
    def boom():
        raise RuntimeError("sk-SECRET")

    monkeypatch.setattr(admin, "embed_live_catalog", boom)
    edited = FakeEditable()
    await admin.on_price_embed(FakeQuery("pe|1", FakeBot(), edited), FakeBot())
    assert edited.edits[-1][0] == "Не удалось посчитать эмбеддинги."
    assert "SECRET" not in edited.edits[-1][0]
    assert admin._embed_running is False
    assert catalog_embed_lock.locked() is False


def test_embed_helpers_do_not_call_the_provider(monkeypatch):
    class FakeEmbedder:
        def __init__(self):
            self.new_texts = 0

        def missing(self, texts):
            assert texts == ["Gloves cotton 10"]
            return ["Gloves cotton 10"]

    def embed_catalog(embedder, names, *, allow_large=None):
        assert allow_large is True
        assert names == ["Gloves cotton 10"]
        embedder.new_texts = 2

    monkeypatch.setattr(admin, "load_catalog", lambda path=None: [SimpleNamespace(name="Gloves cotton 10")])
    monkeypatch.setattr("matching.embeddings.Embedder", FakeEmbedder)
    monkeypatch.setattr("matching.embeddings.embed_catalog", embed_catalog)
    assert admin.count_missing_catalog_names() == 1
    assert admin.embed_live_catalog() == 2


async def test_first_confirm_does_not_invent_a_rollback_copy(session, price_dir, monkeypatch):
    monkeypatch.setattr(admin, "count_missing_catalog_names", lambda: 0)
    bot = FakeBot(payload=_ONE)
    message = await _send(session, bot, "first.xls", len(_ONE))
    replace = message.answers[-1][1].inline_keyboard[0][0].callback_data
    await admin.on_price_upload(FakeQuery(replace, bot, FakeEditable()), session, bot)
    assert (price_dir / "price-list.xls").read_bytes() == _ONE
    assert not (price_dir / "price-list.prev.xls").exists()
