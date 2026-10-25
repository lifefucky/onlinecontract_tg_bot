from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class UserStart(BaseModel):
    """Profile fields taken from Telegram on /start."""

    telegram_user_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str | None = None


class UserRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    telegram_user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    language_code: str | None
    is_banned: bool
    bot_blocked: bool
    profile_cleared: bool
    started_at: datetime | None
    last_seen_at: datetime
    banned_at: datetime | None
    ban_reason: str | None


class UserStats(BaseModel):
    total_started: int = 0
    started_today: int = 0
    seen_7d: int = 0
    banned: int = 0
    blocked_bot: int = 0


class UserPage(BaseModel):
    items: list[UserRow] = Field(default_factory=list)
    total: int = 0
    offset: int = 0
