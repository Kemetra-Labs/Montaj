"""Records stored by the Montaj SQLite database."""

import json
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


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


RuleText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CampaignBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: RuleText
    target_audience: RuleText
    key_messages: list[RuleText] = Field(default_factory=list)
    content_guidance: list[RuleText] = Field(default_factory=list)


class CampaignRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_seconds: float = Field(gt=0, allow_inf_nan=False)
    banned_topics: list[RuleText] = Field(default_factory=list)
    banned_words: list[RuleText] = Field(default_factory=list)
    required_hashtags: list[RuleText] = Field(default_factory=list)
    allowed_sources: list[RuleText] = Field(default_factory=list)
    required_disclosure: str | None = None
    max_posts_per_day: int | None = Field(default=None, ge=1)
    flag_medical_claims: bool = False
    source_must_be_allowed: bool = True
    brief: CampaignBrief | None = None

    def compliance_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"brief"})

    @field_validator("required_disclosure", mode="before")
    @classmethod
    def strip_disclosure(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip() or None

    @model_validator(mode="after")
    def check_duration_range(self) -> Self:
        if self.max_seconds < self.min_seconds:
            raise ValueError("max_seconds must be at least min_seconds")
        return self


class Campaign(Record):
    name: str
    platform: Platform
    rules_json: str
    deadline: date | None = None
    rate_per_1k: float | None = Field(default=None, ge=0)
    min_views: int | None = Field(default=None, ge=0)
    cap: float | None = Field(default=None, ge=0)

    @property
    def rules(self) -> CampaignRules:
        return CampaignRules.model_validate_json(self.rules_json)


class Account(Record):
    platform: Platform
    handle: str
    niche: Niche
    style_notes: str | None = None


class Source(Record):
    url: str | None = None
    path: str | None = None
    title: str
    niche: Niche
    campaign_id: int | None = None
    source_allowed: bool = False
    added_at: datetime = Field(default_factory=utc_now)
    input_key: str | None = None
    requested_language: str | None = None
    speech_status: str | None = None
    failure_note: str | None = None
    allowed_basis: str | None = None
    allowed_at: datetime | None = None


class ClipStatus(StrEnum):
    CANDIDATE = "candidate"
    RANKED = "ranked"
    REJECTED = "rejected"
    RENDERED = "rendered"
    CAPTIONED = "captioned"
    DRAFTED = "drafted"
    CHECKED = "checked"
    NEEDS_FIX = "needs_fix"
    APPROVED = "approved"
    EXPORTED = "exported"
    POSTED = "posted"


class CheckResult(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class ClipStage(StrEnum):
    CUT = "cut"
    CAPTIONS = "captions"
    DRAFT = "draft"
    CHECK = "check"
    REVIEW = "review"
    EXPORT = "export"
    POST_LOG = "post-log"


class Clip(Record):
    source_id: int
    account_id: int | None = None
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    moment_type: str | None = None
    score: float | None = None
    decision_reason: str | None = None
    hook_text: str | None = None
    hook_style: str | None = None
    hook_a: str | None = None
    hook_a_style: str | None = None
    hook_b: str | None = None
    hook_b_style: str | None = None
    hashtags: list[str] = Field(default_factory=list)
    chosen_hook: str | None = None
    caption: str | None = None
    raw_path: str | None = None
    final_path: str | None = None
    status: ClipStatus = ClipStatus.CANDIDATE
    input_hashes: dict[ClipStage, str] = Field(default_factory=dict)
    draft_quotes_clip: bool = True
    check_result: CheckResult | None = None
    check_input_hash: str | None = None
    reviewed_at: datetime | None = None
    reviewed_input_hash: str | None = None
    changed_since_review: bool = False

    @field_validator("input_hashes", "hashtags", mode="before")
    @classmethod
    def decode_json_fields(cls, value: object) -> object:
        return json.loads(value) if isinstance(value, str) else value

    @field_validator("chosen_hook")
    @classmethod
    def check_chosen_hook(cls, value: str | None) -> str | None:
        if value is not None and value not in {"a", "b", "edit"}:
            raise ValueError("chosen_hook must be a, b, or edit")
        return value


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
