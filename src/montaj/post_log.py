"""Record posts that were published by hand. Nothing here uploads."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Any

import typer
from typer.core import TyperGroup

from montaj.config import Settings
from montaj.db import connect, fetch, insert, save_clip
from montaj.export import export_inputs
from montaj.models import Account, Clip, ClipStage, ClipStatus, Post, Source, utc_now
from montaj.pipeline import stage_input_hash


class _PostLogGroup(TyperGroup):
    """Keep `list` as a command and still allow options after the clip id."""

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: Any = None,
        **extra: Any,
    ) -> Any:
        # Click copies this flag before parsing, and an optional clip id would
        # otherwise swallow the `list` command.
        listing = bool(args and args[0] == "list")
        self.allow_interspersed_args = not listing
        saved = self.params
        if listing:
            self.params = [param for param in saved if param.name != "clip_id"]
        try:
            return super().make_context(info_name, args, parent, **extra)
        finally:
            self.params = saved


post_log_app = typer.Typer(cls=_PostLogGroup, invoke_without_command=True)


@dataclass(frozen=True, slots=True)
class LoggedPost:
    post_id: int
    clip_id: int
    account_id: int
    handle: str
    platform: str
    posted_at: str
    url: str
    day: str


def parse_posted_at(value: str) -> datetime:
    """Accept a calendar day or an ISO timestamp. Naive values are UTC."""
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Enter a time as YYYY-MM-DD or an ISO timestamp.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _url(value: str) -> str:
    text = value.strip()
    if " " in text or not text.startswith(("https://", "http://")):
        raise ValueError("Enter an http or https post URL.")
    rest = text.removeprefix("https://").removeprefix("http://")
    if not any(char.isalnum() for char in rest):
        raise ValueError("Enter an http or https post URL.")
    return text


def _with_post_hash(
    settings: Settings, clip: Clip, source: Source | None
) -> dict[ClipStage, str]:
    hashes = dict(clip.input_hashes)
    if source is None:
        return hashes
    try:
        inputs = export_inputs(settings, source)
    except (OSError, ValueError):
        return hashes
    hashes[ClipStage.POST_LOG] = stage_input_hash(clip, ClipStage.POST_LOG, inputs)
    return hashes


def log_post(
    settings: Settings,
    clip_id: int,
    account_id: int,
    url: str,
    *,
    posted_at: datetime | None = None,
) -> Post:
    """Store one manual post and mark the clip posted. Raises ValueError when refused."""
    cleaned = _url(url)
    when = posted_at if posted_at is not None else utc_now()
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    when = when.astimezone(UTC)
    if not settings.db_path.is_file():
        raise ValueError("Database is missing. Run montaj db init first.")
    with connect(settings.db_path) as db:
        clip = fetch(db, Clip, clip_id)
        account = fetch(db, Account, account_id)
        if clip is None:
            raise ValueError(f"Clip {clip_id} does not exist.")
        if account is None or account.id is None:
            raise ValueError(f"Account {account_id} does not exist.")
        already = db.execute(
            "SELECT 1 FROM posts WHERE clip_id = ? AND account_id = ?",
            (clip_id, account.id),
        ).fetchone()
        if already is not None:
            raise ValueError(f"Clip {clip_id} is already posted to this account.")
        if clip.status != ClipStatus.EXPORTED:
            raise ValueError(f"Clip {clip_id} is not exported.")
        source = fetch(db, Source, clip.source_id)
        post = insert(
            db,
            Post(
                clip_id=clip_id,
                account_id=account.id,
                platform=account.platform,
                posted_at=when,
                url=cleaned,
            ),
        )
        save_clip(
            db,
            clip.model_copy(
                update={
                    "status": ClipStatus.POSTED,
                    "input_hashes": _with_post_hash(settings, clip, source),
                }
            ),
        )
    return post


def logged_posts(
    settings: Settings, *, account_id: int | None = None, day: date | None = None
) -> list[LoggedPost]:
    """Return logged posts, optionally for one account and one UTC day."""
    if not settings.db_path.is_file():
        raise ValueError("Database is missing. Run montaj db init first.")
    stamp = day.isoformat() if day is not None else None
    with connect(settings.db_path) as db:
        rows = db.execute(
            """SELECT posts.id, posts.clip_id, posts.account_id, accounts.handle,
                      posts.platform, posts.posted_at, posts.url,
                      substr(posts.posted_at, 1, 10) AS day
               FROM posts
               JOIN accounts ON accounts.id = posts.account_id
               WHERE (? IS NULL OR posts.account_id = ?)
                 AND (? IS NULL OR substr(posts.posted_at, 1, 10) = ?)
               ORDER BY day, posts.account_id, posts.id""",
            (account_id, account_id, stamp, stamp),
        ).fetchall()
    return [
        LoggedPost(
            post_id=int(row["id"]),
            clip_id=int(row["clip_id"]),
            account_id=int(row["account_id"]),
            handle=str(row["handle"]),
            platform=str(row["platform"]),
            posted_at=str(row["posted_at"]),
            url=str(row["url"]),
            day=str(row["day"]),
        )
        for row in rows
    ]


def format_post_log(rows: Sequence[LoggedPost]) -> str:
    """Show each post and the count for that account on that day."""
    if not rows:
        return "No posts logged."
    lines: list[str] = []
    index = 0
    while index < len(rows):
        day = rows[index].day
        account_id = rows[index].account_id
        handle = rows[index].handle
        batch: list[LoggedPost] = []
        while (
            index < len(rows)
            and rows[index].day == day
            and rows[index].account_id == account_id
        ):
            batch.append(rows[index])
            index += 1
        noun = "post" if len(batch) == 1 else "posts"
        lines.append(f"{day}  account {account_id} @{handle}  {len(batch)} {noun}")
        for item in batch:
            lines.append(
                f"  clip {item.clip_id}  {item.platform}  {item.posted_at}  {item.url}"
            )
    return "\n".join(lines)


@post_log_app.callback(invoke_without_command=True)
def post_log(
    ctx: typer.Context,
    clip_id: Annotated[int | None, typer.Argument(min=1)] = None,
    account: Annotated[int | None, typer.Option("--account", min=1)] = None,
    url: Annotated[str | None, typer.Option("--url")] = None,
    at: Annotated[str | None, typer.Option("--at")] = None,
) -> None:
    """Record a post that was published by hand."""
    if ctx.invoked_subcommand is not None:
        return
    if clip_id is None or account is None or not url:
        typer.echo(
            "Usage: montaj post-log CLIP_ID --account ID --url POST_URL [--at TIME]",
            err=True,
        )
        raise typer.Exit(2)
    try:
        posted = log_post(
            Settings.from_env(),
            clip_id,
            account,
            url,
            posted_at=parse_posted_at(at) if at else None,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        typer.echo(f"Post log failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Logged post {posted.id} for clip {posted.clip_id}.")


@post_log_app.command("list")
def list_posts(
    account: Annotated[int | None, typer.Option("--account", min=1)] = None,
    today: Annotated[bool, typer.Option("--today")] = False,
) -> None:
    """Show logged posts and how many each account has per day."""
    try:
        day = datetime.now(UTC).date() if today else None
        rows = logged_posts(Settings.from_env(), account_id=account, day=day)
    except (OSError, sqlite3.Error, ValueError) as exc:
        typer.echo(f"Post log failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(format_post_log(rows))
