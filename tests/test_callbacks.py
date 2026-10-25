"""Pure callback parsing (no Bot / aiogram)."""

import pytest

from utils.callbacks import (
    parse_delete_user_callback,
    parse_match_feedback,
    parse_pagination_callback,
    parse_users_page,
)


def test_parse_pagination_callback_ok():
    offset, cat = parse_pagination_callback("m|7|1811,1817")
    assert offset == 7
    assert cat == "1811,1817"


def test_parse_pagination_callback_bad_input():
    with pytest.raises((ValueError, IndexError)):
        parse_pagination_callback("not-m-format")


def test_parse_admin_callbacks():
    assert parse_users_page("au|16") == 16
    assert parse_delete_user_callback("adel|42") == ("adel", 42)
    assert parse_delete_user_callback("adelx|42") == ("adelx", 42)
    with pytest.raises(ValueError):
        parse_users_page("au|-1")
    with pytest.raises(ValueError):
        parse_delete_user_callback("other|1")


def test_parse_match_feedback():
    assert parse_match_feedback("mf|588845|1|abcd1234") == (588845, "fit", "abcd1234")
    assert parse_match_feedback("mf|12|0|deadbeef") == (12, "reject", "deadbeef")
    with pytest.raises(ValueError):
        parse_match_feedback("mf|0|1|abcd")
    with pytest.raises(ValueError):
        parse_match_feedback("mf|12|2|abcd")
    with pytest.raises(ValueError):
        parse_match_feedback("mf|12|1|nope")
