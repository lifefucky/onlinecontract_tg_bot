from enum import StrEnum


class MessageTrackScope(StrEnum):
    """Tracked bot message groups for UI cleanup (extensible)."""

    procedures = "procedures"
    positions = "positions"
    welcome = "welcome"


class StartResult(StrEnum):
    """Outcome of recording /start for one Telegram user."""

    created = "created"
    updated = "updated"
    banned = "banned"
