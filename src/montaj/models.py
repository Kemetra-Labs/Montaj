"""Records stored by the Montaj SQLite database."""

from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class Niche(StrEnum):
    BUSINESS = "business"
    HEALTH = "health"
    GAMES = "games"


class Platform(StrEnum):
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"


class Record(BaseModel):
    id: int | None = None


class Campaign(Record):
    name: str
    platform: Platform
    rules_json: str
    deadline: date | None = None
    rate_per_1k: float | None = Field(default=None, ge=0)
    min_views: int | None = Field(default=None, ge=0)
    cap: float | None = Field(default=None, ge=0)


class Account(Record):
    platform: Platform
    handle: str
    niche: Niche


class Source(Record):
    url: str | None = None
    path: str | None = None
    title: str
    niche: Niche
    campaign_id: int | None = None
    source_allowed: bool = False
    added_at: datetime = Field(default_factory=utc_now)


class Clip(Record):
    source_id: int
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    moment_type: str | None = None
    score: float | None = None
    hook_text: str | None = None
    hook_style: str | None = None
    caption: str | None = None
    raw_path: str | None = None
    final_path: str | None = None
    status: str = "candidate"


class Post(Record):
    clip_id: int
    account_id: int
    platform: Platform
    posted_at: datetime = Field(default_factory=utc_now)
    url: str


class Metric(Record):
    post_id: int
    taken_at: datetime = Field(default_factory=utc_now)
    views: int = Field(ge=0)
    likes: int | None = Field(default=None, ge=0)
    comments: int | None = Field(default=None, ge=0)
    shares: int | None = Field(default=None, ge=0)


class Earning(Record):
    post_id: int
    campaign_id: int
    amount_usd: float = Field(ge=0)
    status: str = "pending"
    paid_at: datetime | None = None
