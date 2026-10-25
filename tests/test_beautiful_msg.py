"""Tests for tgbot/ui/messages.py (no network)."""

from tgbot.ui.messages import (
    beautiful_positions,
    beautiful_procedure,
    split_telegram_messages,
)


def test_beautiful_procedure_contains_key_parts():
    row = {
        "owner": "Org",
        "id": 42,
        "link": "https://onlinecontract.ru/tenders/42",
        "name": "Soap",
        "useNDS": True,
        "nds": 20,
        "reBiddingStart": "2025-01-01",
        "reBiddingEnd": "2025-01-02",
    }
    card = beautiful_procedure(row)
    assert "Org" in card
    assert "42" in card
    assert "Soap" in card
    assert "20 %" in card
    assert "https://onlinecontract.ru/tenders/42" in card


def test_beautiful_positions_empty_positions_message():
    data = {
        "common_message": {"Id": "99"},
        "positions": [],
    }
    out = beautiful_positions(data)
    assert "КЛ-" in out
    assert "99" in out
    assert "Позиции не указаны" in out


def test_beautiful_positions_with_lines():
    data = {
        "common_message": {
            "Id": "1",
            "ownerSrok": "100%",
            "deliveryPlace": "Moscow",
        },
        "positions": [
            {"name": "Item", "totalCount": 2, "unit": "pcs", "price": 10},
        ],
    }
    out = beautiful_positions(data)
    assert "Место поставки" in out
    assert "Item" in out
    assert "pcs" in out


def test_split_telegram_messages_short_unchanged():
    text = "hello"
    assert split_telegram_messages(text) == ["hello"]


def test_split_telegram_messages_respects_max_len():
    long_line = "x" * 5000
    chunks = split_telegram_messages(long_line, max_len=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == long_line


def test_split_telegram_messages_prefers_newline():
    text = "a\n" * 3000
    chunks = split_telegram_messages(text, max_len=50)
    assert all(len(c) <= 50 for c in chunks)
    assert "".join(chunks) == text
