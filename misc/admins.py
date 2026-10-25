"""Owner ids and parsing of admin command arguments."""

from os import environ


def get_admin_ids() -> frozenset[int]:
    """Telegram user ids allowed to run owner commands. Empty if ADMIN_IDS is unset."""
    raw = environ.get("ADMIN_IDS", "").strip()
    if not raw:
        return frozenset()
    ids: set[int] = set()
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        try:
            value = int(piece)
        except ValueError as exc:
            raise RuntimeError(f"ADMIN_IDS contains a non-integer value: {piece!r}") from exc
        if value <= 0:
            raise RuntimeError(f"ADMIN_IDS contains a non-positive id: {value}")
        ids.add(value)
    return frozenset(ids)


def parse_id_and_reason(args: str | None) -> tuple[int, str | None] | None:
    """Parse ``<telegram user id> [reason]``. Returns None when the id is missing or not an integer."""
    if args is None or not args.strip():
        return None
    head, _, tail = args.strip().partition(" ")
    try:
        user_id = int(head)
    except ValueError:
        return None
    if user_id <= 0:
        return None
    reason = tail.strip()
    if not reason:
        return user_id, None
    return user_id, reason[:255]
