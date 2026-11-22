import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from montaj.cli import app
from montaj.db import (
    SCHEMA_VERSION,
    SchemaVersionError,
    connect,
    fetch,
    init_db,
    insert,
)
from montaj.models import (
    Account,
    Campaign,
    Clip,
    Earning,
    Metric,
    Niche,
    Platform,
    Post,
    Source,
)


def test_records_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "montaj.db"
    init_db(path)
    with connect(path) as db:
        campaign = insert(
            db, Campaign(name="Test", platform=Platform.YOUTUBE, rules_json="{}")
        )
        assert campaign.id is not None
        account = insert(
            db, Account(platform=Platform.YOUTUBE, handle="test", niche=Niche.GAMES)
        )
        assert account.id is not None
        source = insert(
            db,
            Source(
                path="test.mp4",
                title="Test",
                niche=Niche.GAMES,
                campaign_id=campaign.id,
            ),
        )
        assert source.id is not None
        assert source.source_allowed is False
        clip = insert(db, Clip(source_id=source.id, start_s=1, end_s=3))
        assert clip.id is not None
        post = insert(
            db,
            Post(
                clip_id=clip.id,
                account_id=account.id,
                platform=account.platform,
                url="https://example.test/post",
            ),
        )
        assert post.id is not None
        metric = insert(db, Metric(post_id=post.id, views=100))
        earning = insert(
            db, Earning(post_id=post.id, campaign_id=campaign.id, amount_usd=1)
        )

    init_db(path)
    with connect(path) as db:
        for record in (campaign, account, source, clip, post, metric, earning):
            assert record.id is not None
            assert fetch(db, type(record), record.id) == record
        assert fetch(db, Source, source.id + 1) is None
        assert (
            db.execute("SELECT version FROM schema_version").fetchone()[0]
            == SCHEMA_VERSION
        )


def test_foreign_keys_and_rollback(tmp_path: Path) -> None:
    path = tmp_path / "montaj.db"
    init_db(path)
    with (
        pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"),
        connect(path) as db,
    ):
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        insert(
            db,
            Account(id=1, platform=Platform.TIKTOK, handle="tmp", niche=Niche.HEALTH),
        )
        insert(db, Clip(source_id=999, start_s=0, end_s=1))
    with connect(path) as db:
        assert fetch(db, Account, 1) is None


def test_database_init_respects_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "subfolder"
    monkeypatch.setenv("MONTAJ_DATA_DIR", str(data_dir))
    result = CliRunner().invoke(app, ["db", "init"])
    assert result.exit_code == 0, result.output
    assert (data_dir / "montaj.db").is_file()


def test_rejects_unknown_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "montaj.db"
    init_db(path)
    with connect(path) as db:
        db.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION + 1,))
    with pytest.raises(SchemaVersionError, match="SQLite schema version"):
        init_db(path)
