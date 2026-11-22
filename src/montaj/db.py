"""SQLite connections, schema setup, and record persistence."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from montaj.models import Account, Campaign, Clip, Earning, Metric, Post, Record, Source

SCHEMA_VERSION = 8
_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    deadline TEXT,
    rate_per_1k REAL,
    min_views INTEGER,
    cap REAL
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    handle TEXT NOT NULL,
    niche TEXT NOT NULL,
    UNIQUE (platform, handle)
);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    url TEXT,
    path TEXT,
    title TEXT NOT NULL,
    niche TEXT NOT NULL,
    campaign_id INTEGER REFERENCES campaigns(id),
    source_allowed INTEGER NOT NULL DEFAULT 0 CHECK (source_allowed IN (0, 1)),
    added_at TEXT NOT NULL,
    CHECK (url IS NOT NULL OR path IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    start_s REAL NOT NULL CHECK (start_s >= 0),
    end_s REAL NOT NULL CHECK (end_s > start_s),
    moment_type TEXT,
    score REAL,
    hook_text TEXT,
    hook_style TEXT,
    caption TEXT,
    raw_path TEXT,
    final_path TEXT,
    status TEXT NOT NULL DEFAULT 'candidate'
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL REFERENCES clips(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    platform TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    url TEXT NOT NULL,
    UNIQUE (clip_id, account_id)
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    taken_at TEXT NOT NULL,
    views INTEGER NOT NULL CHECK (views >= 0),
    likes INTEGER CHECK (likes >= 0),
    comments INTEGER CHECK (comments >= 0),
    shares INTEGER CHECK (shares >= 0)
);
CREATE TABLE IF NOT EXISTS earnings (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    amount_usd REAL NOT NULL CHECK (amount_usd >= 0),
    status TEXT NOT NULL CHECK (status IN ('pending', 'paid')),
    paid_at TEXT
);
"""

_QUERIES: dict[type[Record], tuple[str, str]] = {
    Campaign: (
        """INSERT INTO campaigns (id, name, platform, rules_json, deadline, rate_per_1k,
           min_views, cap) VALUES (:id, :name, :platform, :rules_json, :deadline,
           :rate_per_1k, :min_views, :cap) RETURNING *""",
        "SELECT * FROM campaigns WHERE id = ?",
    ),
    Account: (
        """INSERT INTO accounts (id, platform, handle, niche, style_notes)
           VALUES (:id, :platform, :handle, :niche, :style_notes) RETURNING *""",
        "SELECT * FROM accounts WHERE id = ?",
    ),
    Source: (
        """INSERT INTO sources (id, url, path, title, niche, campaign_id, source_allowed,
           added_at, input_key) VALUES (:id, :url, :path, :title, :niche, :campaign_id,
           :source_allowed, :added_at, :input_key) RETURNING *""",
        "SELECT * FROM sources WHERE id = ?",
    ),
    Clip: (
        """INSERT INTO clips (id, source_id, start_s, end_s, moment_type, score, hook_text,
           hook_style, hook_a, hook_a_style, hook_b, hook_b_style, hashtags, chosen_hook,
           caption, raw_path, final_path, status, input_hashes, draft_quotes_clip,
           check_result, check_input_hash, reviewed_at, reviewed_input_hash, changed_since_review,
           decision_reason)
           VALUES (:id, :source_id, :start_s, :end_s, :moment_type, :score, :hook_text,
           :hook_style, :hook_a, :hook_a_style, :hook_b, :hook_b_style, :hashtags, :chosen_hook,
           :caption, :raw_path, :final_path, :status, :input_hashes, :draft_quotes_clip,
           :check_result, :check_input_hash, :reviewed_at, :reviewed_input_hash,
           :changed_since_review, :decision_reason) RETURNING *""",
        "SELECT * FROM clips WHERE id = ?",
    ),
    Post: (
        """INSERT INTO posts (id, clip_id, account_id, platform, posted_at, url)
           VALUES (:id, :clip_id, :account_id, :platform, :posted_at, :url) RETURNING *""",
        "SELECT * FROM posts WHERE id = ?",
    ),
    Metric: (
        """INSERT INTO metrics (id, post_id, taken_at, views, likes, comments, shares)
           VALUES (:id, :post_id, :taken_at, :views, :likes, :comments, :shares) RETURNING *""",
        "SELECT * FROM metrics WHERE id = ?",
    ),
    Earning: (
        """INSERT INTO earnings (id, post_id, campaign_id, amount_usd, status, paid_at)
           VALUES (:id, :post_id, :campaign_id, :amount_usd, :status, :paid_at) RETURNING *""",
        "SELECT * FROM earnings WHERE id = ?",
    ),
}

_CLIP_MIGRATION = (
    "ALTER TABLE clips ADD COLUMN input_hashes TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE clips ADD COLUMN draft_quotes_clip INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE clips ADD COLUMN check_result TEXT CHECK (check_result IN ('pass', 'warn', 'fail'))",
    "ALTER TABLE clips ADD COLUMN check_input_hash TEXT",
    "ALTER TABLE clips ADD COLUMN reviewed_at TEXT",
    "ALTER TABLE clips ADD COLUMN reviewed_input_hash TEXT",
    "ALTER TABLE clips ADD COLUMN changed_since_review INTEGER NOT NULL DEFAULT 0",
)

_COST_MIGRATION = (
    """CREATE TABLE api_calls (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL REFERENCES sources(id),
        stage TEXT NOT NULL,
        service TEXT NOT NULL CHECK (service IN ('deepgram', 'claude')),
        input_tokens INTEGER CHECK (input_tokens >= 0),
        output_tokens INTEGER CHECK (output_tokens >= 0),
        seconds REAL CHECK (seconds >= 0),
        estimated_cost_usd REAL NOT NULL CHECK (estimated_cost_usd >= 0),
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX api_calls_source_stage ON api_calls (source_id, stage)",
)

_BUDGET_MIGRATION = (
    "ALTER TABLE api_calls ADD COLUMN run_id TEXT",
    "ALTER TABLE api_calls ADD COLUMN outcome TEXT NOT NULL DEFAULT 'success'",
    "CREATE INDEX api_calls_run ON api_calls (run_id)",
)

_MIGRATIONS = {
    1: _CLIP_MIGRATION,
    2: _COST_MIGRATION,
    3: _BUDGET_MIGRATION,
    4: ("ALTER TABLE accounts ADD COLUMN style_notes TEXT",),
    5: (
        "ALTER TABLE sources ADD COLUMN input_key TEXT",
        "CREATE UNIQUE INDEX sources_input_key ON sources(input_key)",
    ),
    6: ("ALTER TABLE clips ADD COLUMN decision_reason TEXT",),
    7: (
        "ALTER TABLE clips ADD COLUMN hook_a TEXT",
        "ALTER TABLE clips ADD COLUMN hook_a_style TEXT",
        "ALTER TABLE clips ADD COLUMN hook_b TEXT",
        "ALTER TABLE clips ADD COLUMN hook_b_style TEXT",
        "ALTER TABLE clips ADD COLUMN hashtags TEXT NOT NULL DEFAULT '[]'",
        """ALTER TABLE clips ADD COLUMN chosen_hook TEXT
           CHECK (chosen_hook IN ('a', 'b', 'edit'))""",
    ),
}


class SchemaVersionError(ValueError):
    """The database schema is newer or older than this build supports."""


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a transaction scope, with foreign keys enabled on this connection."""
    db = sqlite3.connect(path)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        with db:
            yield db
    finally:
        db.close()


def init_db(path: Path) -> None:
    """Create the database and record its schema version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.executescript(_SCHEMA)
        db.execute(
            "INSERT INTO schema_version (id, version) VALUES (1, ?) ON CONFLICT DO NOTHING",
            (1,),
        )
        version = db.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()[0]
        while version in _MIGRATIONS:
            for statement in _MIGRATIONS[version]:
                db.execute(statement)
            version += 1
            db.execute("UPDATE schema_version SET version = ? WHERE id = 1", (version,))
        if version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"SQLite schema version {version}, expected {SCHEMA_VERSION}."
            )


def _record_values(record: Record) -> dict[str, Any]:
    values = record.model_dump(mode="json")
    if isinstance(record, Clip):
        values["input_hashes"] = json.dumps(values["input_hashes"], sort_keys=True)
        values["hashtags"] = json.dumps(values["hashtags"], ensure_ascii=False)
    return values


def insert[T: Record](db: sqlite3.Connection, record: T) -> T:
    """Insert a typed record and return it with its database-assigned ID."""
    model = type(record)
    insert_sql, _ = _QUERIES[model]
    row = db.execute(insert_sql, _record_values(record)).fetchone()
    return model.model_validate(dict(row))


def fetch[T: Record](
    db: sqlite3.Connection, model: type[T], record_id: int
) -> T | None:
    """Load a typed record by ID, or return None when it is absent."""
    _, select_sql = _QUERIES[model]
    row = db.execute(select_sql, (record_id,)).fetchone()
    return model.model_validate(dict(row)) if row is not None else None


def save_clip(db: sqlite3.Connection, clip: Clip) -> None:
    """Persist a clip's stage outputs and review state together."""
    updated = db.execute(
        """UPDATE clips SET start_s = :start_s, end_s = :end_s, moment_type = :moment_type,
           score = :score, hook_text = :hook_text, hook_style = :hook_style,
           hook_a = :hook_a, hook_a_style = :hook_a_style, hook_b = :hook_b,
           hook_b_style = :hook_b_style, hashtags = :hashtags, chosen_hook = :chosen_hook,
           caption = :caption,
           raw_path = :raw_path, final_path = :final_path, status = :status,
           input_hashes = :input_hashes, draft_quotes_clip = :draft_quotes_clip,
           check_result = :check_result, check_input_hash = :check_input_hash,
           reviewed_at = :reviewed_at, reviewed_input_hash = :reviewed_input_hash,
           changed_since_review = :changed_since_review, decision_reason = :decision_reason
           WHERE id = :id AND source_id = :source_id
        """,
        _record_values(clip),
    )
    if updated.rowcount != 1:
        raise ValueError(f"Clip {clip.id} does not exist for source {clip.source_id}.")
