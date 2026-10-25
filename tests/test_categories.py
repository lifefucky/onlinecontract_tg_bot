"""Categories callback_data must match tgbot.user.CATEGORY_CALLBACK_IDS."""

from tgbot.ui.keyboards import onlc_text_and_data
from tgbot.user import CATEGORY_CALLBACK_IDS


def test_category_callback_ids_match_bot():
    expected = frozenset(data for _, data in onlc_text_and_data)
    assert CATEGORY_CALLBACK_IDS == expected
