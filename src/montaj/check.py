"""Check a drafted clip against its campaign rules before review."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from montaj.captions import source_inputs
from montaj.config import Settings
from montaj.cut import selected_clips
from montaj.db import connect, fetch, save_clip
from montaj.ingest import load_source
from montaj.media import MediaError, source_lock
from montaj.models import (
    Account,
    Campaign,
    CampaignRules,
    CheckResult,
    Clip,
    ClipStage,
    ClipStatus,
    Niche,
    Source,
)
from montaj.pipeline import input_hash, stage_input_hash
from montaj.rank import banned_term, clip_words
from montaj.transcribe import Transcript

_MEDICAL = Path("config/medical_claims.txt")


@dataclass(frozen=True, slots=True)
class RuleFinding:
    result: CheckResult
    reason: str
    simple_fix: bool = False


@dataclass(frozen=True, slots=True)
class ClipCheck:
    result: CheckResult
    reasons: tuple[str, ...]
    simple_fix: bool


def medical_terms() -> list[str]:
    if not _MEDICAL.is_file():
        raise ValueError("Medical-claim list is missing at config/medical_claims.txt.")
    terms = []
    for line in _MEDICAL.read_text(encoding="utf-8").splitlines():
        phrase = line.split("#", 1)[0].strip()
        if phrase:
            terms.append(phrase)
    if not terms:
        raise ValueError("Medical-claim list is empty.")
    return terms


def _duration(clip: Clip, rules: CampaignRules) -> RuleFinding | None:
    length = clip.end_s - clip.start_s
    if rules.min_seconds - 1e-8 <= length <= rules.max_seconds + 1e-8:
        return None
    return RuleFinding(
        CheckResult.FAIL,
        (
            f"Length {length:.2f}s is outside "
            f"{rules.min_seconds:.2f}-{rules.max_seconds:.2f}s."
        ),
    )


def _missing(needles: list[str], haystack: str, label: str) -> list[RuleFinding]:
    folded = haystack.casefold()
    return [
        RuleFinding(CheckResult.FAIL, f"Missing {label}: {needle}", simple_fix=True)
        for needle in needles
        if needle.casefold() not in folded
    ]


def check_clip(
    clip: Clip,
    rules: CampaignRules,
    *,
    transcript_text: str,
    niche: Niche,
    source_allowed: bool,
    posts_today: int | None,
    already_posted: bool,
    medical: list[str],
) -> ClipCheck:
    """Return the worst rule result and the reasons a reviewer should see."""
    published = " ".join(
        part
        for part in (
            clip.hook_text,
            clip.hook_a,
            clip.hook_b,
            clip.caption,
            " ".join(clip.hashtags),
        )
        if part
    )
    searchable = f"{transcript_text}\n{published}"
    findings: list[RuleFinding] = []
    if finding := _duration(clip, rules):
        findings.append(finding)
    if term := banned_term(searchable, rules):
        findings.append(
            RuleFinding(CheckResult.FAIL, f"Matches banned word or topic: {term}")
        )
    findings.extend(
        _missing(list(rules.required_hashtags), published, "required hashtag")
    )
    if rules.required_disclosure:
        findings.extend(
            _missing([rules.required_disclosure], clip.caption or "", "disclosure")
        )
    if rules.source_must_be_allowed and not source_allowed:
        findings.append(RuleFinding(CheckResult.FAIL, "Source is not permitted."))
    if already_posted:
        findings.append(RuleFinding(CheckResult.FAIL, "This clip was already posted."))
    if rules.max_posts_per_day is not None:
        if posts_today is None:
            findings.append(
                RuleFinding(
                    CheckResult.WARN,
                    "No matching account to apply the daily post cap.",
                )
            )
        elif cap_failure := daily_cap_failure(rules, posts_today):
            findings.append(cap_failure)
    if rules.flag_medical_claims or niche == Niche.HEALTH:
        flagged = banned_term(
            published,
            rules.model_copy(update={"banned_words": medical, "banned_topics": []}),
        )
        if flagged:
            findings.append(
                RuleFinding(
                    CheckResult.WARN, f"Possible medical claim or promise: {flagged}"
                )
            )
    if not findings:
        return ClipCheck(CheckResult.PASS, (), False)
    rank = {CheckResult.PASS: 0, CheckResult.WARN: 1, CheckResult.FAIL: 2}
    result = max((item.result for item in findings), key=rank.__getitem__)
    fails = [item for item in findings if item.result == CheckResult.FAIL]
    return ClipCheck(
        result,
        tuple(item.reason for item in findings),
        bool(fails) and all(item.simple_fix for item in fails),
    )


def daily_cap_failure(rules: CampaignRules, count: int) -> RuleFinding | None:
    if rules.max_posts_per_day is None or count < rules.max_posts_per_day:
        return None
    return RuleFinding(
        CheckResult.FAIL,
        f"Daily post cap is {rules.max_posts_per_day}; {count} posts are already logged today.",
    )


def posts_today(db: sqlite3.Connection, account_id: int) -> int:
    today = datetime.now(UTC).date().isoformat()
    row = db.execute(
        """SELECT COUNT(*) FROM posts
           WHERE account_id = ? AND substr(posted_at, 1, 10) = ?""",
        (account_id, today),
    ).fetchone()
    return int(row[0])


def _account_for_cap(
    db: sqlite3.Connection, source: Source, account_id: int | None
) -> Account | None:
    if account_id is not None:
        account = fetch(db, Account, account_id)
        if account is None:
            raise ValueError(f"Account {account_id} does not exist.")
        return account
    if source.campaign_id is None:
        return None
    campaign = fetch(db, Campaign, source.campaign_id)
    if campaign is None:
        raise ValueError(f"Campaign {source.campaign_id} does not exist.")
    rows = db.execute(
        "SELECT id FROM accounts WHERE niche = ? AND platform = ? ORDER BY id",
        (source.niche, campaign.platform),
    ).fetchall()
    if len(rows) != 1:
        return None
    return fetch(db, Account, int(rows[0][0]))


def _store(clip: Clip, outcome: ClipCheck, key: str) -> None:
    clip.check_result = outcome.result
    clip.check_input_hash = key
    if outcome.result == CheckResult.PASS:
        if clip.status not in (
            ClipStatus.APPROVED,
            ClipStatus.EXPORTED,
            ClipStatus.POSTED,
        ):
            clip.status = ClipStatus.CHECKED
        return
    if clip.status == ClipStatus.POSTED:
        return
    clip.decision_reason = "; ".join(outcome.reasons)
    if outcome.result == CheckResult.WARN and clip.status in (
        ClipStatus.APPROVED,
        ClipStatus.EXPORTED,
    ):
        clip.status = ClipStatus.CHECKED
        return
    clip.status = (
        ClipStatus.NEEDS_FIX
        if outcome.result == CheckResult.FAIL
        else ClipStatus.CHECKED
    )


def check_source(
    settings: Settings,
    source_id: int,
    *,
    clip_id: int | None = None,
    account_id: int | None = None,
    wait: float = 0,
) -> list[tuple[Clip, ClipCheck]]:
    source, _, directory = load_source(settings, source_id)
    with connect(settings.db_path) as db:
        campaign = (
            fetch(db, Campaign, source.campaign_id)
            if source.campaign_id is not None
            else None
        )
        account = _account_for_cap(db, source, account_id)
        posted_today = (
            posts_today(db, account.id) if account is not None and account.id else None
        )
    if source.campaign_id is not None and campaign is None:
        raise ValueError(f"Campaign {source.campaign_id} does not exist.")
    rules = (
        campaign.rules
        if campaign is not None
        else CampaignRules(min_seconds=20, max_seconds=60)
    )
    terms = medical_terms()
    outcomes: list[tuple[Clip, ClipCheck]] = []
    with source_lock(directory, command="check", wait=wait):
        clips = selected_clips(settings, source_id, directory, clip_id)
        transcript = Transcript.model_validate_json(
            (directory / "transcript.json").read_bytes()
        )
        inputs = source_inputs(
            settings,
            directory,
            rules_hash=input_hash(rules.compliance_payload()),
        )
        for clip in clips:
            if clip.id is None or clip.caption is None or not clip.hook_a:
                raise ValueError(f"Clip {clip.id} has no draft. Run montaj draft.")
            text = " ".join(
                word.text for word in clip_words(transcript, clip.start_s, clip.end_s)
            )
            with connect(settings.db_path) as db:
                already = (
                    db.execute(
                        "SELECT 1 FROM posts WHERE clip_id = ? LIMIT 1", (clip.id,)
                    ).fetchone()
                    is not None
                )
            outcome = check_clip(
                clip,
                rules,
                transcript_text=text,
                niche=source.niche,
                source_allowed=source.source_allowed,
                posts_today=posted_today,
                already_posted=already,
                medical=terms,
            )
            _store(clip, outcome, stage_input_hash(clip, ClipStage.CHECK, inputs))
            with connect(settings.db_path) as db:
                save_clip(db, clip)
            outcomes.append((clip, outcome))
    return outcomes


def check(
    source_id: Annotated[int, typer.Argument(min=1)],
    clip: Annotated[int | None, typer.Option(min=1)] = None,
    account: Annotated[int | None, typer.Option(min=1)] = None,
    wait: Annotated[
        float,
        typer.Option("--wait", min=0, help="Seconds to wait if this source is locked."),
    ] = 0,
) -> None:
    """Check drafted clips against campaign rules."""
    try:
        outcomes = check_source(
            Settings.from_env(),
            source_id,
            clip_id=clip,
            account_id=account,
            wait=wait,
        )
    except (MediaError, OSError, ValueError, sqlite3.Error) as exc:
        typer.echo(f"Check failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo("Clip\tResult\tReasons")
    counts = {result: 0 for result in CheckResult}
    for checked, outcome in outcomes:
        counts[outcome.result] += 1
        typer.echo(
            f"{checked.id}\t{outcome.result.value}\t{'; '.join(outcome.reasons)}"
        )
    typer.echo(
        f"Source {source_id}: {counts[CheckResult.PASS]} pass, "
        f"{counts[CheckResult.WARN]} warn, {counts[CheckResult.FAIL]} fail."
    )
