"""Pure helpers for Telegram callback_data (testable without Bot)."""


def parse_pagination_callback(data: str) -> tuple[int, str]:
    """
    Parse ``m|<offset>|<cat_str>`` from «Показать ещё».
    Raises ValueError / IndexError on bad input (same as naive split).
    """
    _, off_s, cat_str = data.split("|", 2)
    return int(off_s), cat_str


def parse_users_page(data: str) -> int:
    """Parse ``au|<offset>`` for the owner user list. Offset is >= 0."""
    prefix, offset_s = data.split("|", 1)
    if prefix != "au":
        raise ValueError(data)
    offset = int(offset_s)
    if offset < 0:
        raise ValueError(offset)
    return offset


def parse_match_feedback(data: str) -> tuple[int, str, str]:
    """Parse ``mf|<procedure_id>|1|<hash>`` (подходит) or ``|0|`` (не подходит)."""
    kind, raw_id, flag, token = data.split("|", 3)
    if kind != "mf":
        raise ValueError(data)
    procedure_id = int(raw_id)
    if procedure_id <= 0:
        raise ValueError(procedure_id)
    if flag not in {"0", "1"}:
        raise ValueError(flag)
    if len(token) < 4 or any(char not in "0123456789abcdef" for char in token):
        raise ValueError(token)
    decision = "fit" if flag == "1" else "reject"
    return procedure_id, decision, token


def parse_delete_user_callback(data: str) -> tuple[str, int]:
    """Parse ``adel|<id>`` (confirm) or ``adelx|<id>`` (cancel)."""
    action, raw_id = data.split("|", 1)
    if action not in {"adel", "adelx"}:
        raise ValueError(data)
    user_id = int(raw_id)
    if user_id <= 0:
        raise ValueError(user_id)
    return action, user_id


def parse_price_upload_callback(data: str) -> tuple[str, str]:
    """Parse ``pp|<token>|ok`` (replace) or ``pp|<token>|x`` (cancel).

    ``token`` is 8 lowercase hex characters. The whole payload stays under 64 bytes.
    """
    kind, token, action = data.split("|", 2)
    if kind != "pp":
        raise ValueError(data)
    if len(token) != 8 or any(char not in "0123456789abcdef" for char in token):
        raise ValueError(token)
    if action not in {"ok", "x"}:
        raise ValueError(action)
    return action, token


def parse_price_embed_callback(data: str) -> str:
    """Parse ``pe|1``, the owner's button that embeds the live catalog."""
    kind, flag = data.split("|", 1)
    if kind != "pe" or flag != "1":
        raise ValueError(data)
    return flag
