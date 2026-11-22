import json
import subprocess
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from montaj import doctor
from montaj.cli import app
from montaj.config import Settings
from montaj.db import connect


def test_doctor_reports_versions_and_redacts_secrets(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "deepgram-secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret-value")
    monkeypatch.setattr(doctor.shutil, "which", lambda command: f"/test/{command}")

    def version(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, f"{Path(args[0]).name} 1.2.3\n", "")

    def offline(request: Request, timeout: float) -> object:
        raise AssertionError("plain doctor must not call the network")

    monkeypatch.setattr(doctor.subprocess, "run", version)
    monkeypatch.setattr(doctor, "urlopen", offline)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    for name in ("ffmpeg", "ffprobe", "yt-dlp"):
        assert f"{name}: {name} 1.2.3" in result.output
    assert "pinned 2026.10.26" in result.output
    assert "yt-dlp -U" in result.output
    for name in ("DEEPGRAM_API_KEY", "ANTHROPIC_API_KEY"):
        assert f"{name}: set" in result.output
    assert "secret-value" not in result.output
    assert "SQLite: schema version" in result.output
    assert Settings().resolved_font_path.name in result.output


def test_doctor_reports_missing_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invalid_dir = tmp_path / "not-a-directory"
    invalid_dir.touch()
    monkeypatch.setenv("MONTAJ_DATA_DIR", str(invalid_dir))
    monkeypatch.setenv("MONTAJ_FONT_PATH", str(tmp_path / "missing.ttf"))
    monkeypatch.setattr(doctor.shutil, "which", lambda command: None)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert result.output.count("FAIL") == 8
    assert "writable directory" in result.output
    assert "montaj db init" in result.output


def test_database_schema_is_checked(db_path: Path) -> None:
    with connect(db_path) as db:
        db.execute("UPDATE schema_version SET version = 999")
    result = {
        check.name: check for check in doctor.checks(Settings(data_dir=db_path.parent))
    }
    assert not result["SQLite"].ok
    assert "Schema 999" in result["SQLite"].detail


class _Body:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.raw = json.dumps(payload).encode()

    def __enter__(self) -> "_Body":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.raw


class _Opener:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.requests: list[Request] = []

    def __call__(self, request: Request, timeout: float) -> Any:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _http(status: int, body: str) -> HTTPError:
    return HTTPError(
        "https://example.test", status, "status", Message(), BytesIO(body.encode())
    )


def _settings(db_path: Path, *, run_budget: float = 0.5) -> Settings:
    return Settings(
        data_dir=db_path.parent,
        deepgram_api_key=SecretStr("deepgram-secret-value"),
        anthropic_api_key=SecretStr("anthropic-secret-value"),
        max_cost_per_run_usd=run_budget,
        max_cost_per_source_usd=run_budget,
    )


def _ok_replies() -> list[_Body]:
    return [
        _Body({"metadata": {"duration": 1}}),
        _Body({"usage": {"input_tokens": 8, "output_tokens": 1}}),
    ]


def test_online_doctor_logs_a_sub_cent_probe_and_hides_keys(db_path: Path) -> None:
    opener = _Opener(_ok_replies())
    results, cost = doctor.online_checks(_settings(db_path), opener)
    by_name = {check.name: check for check in results}
    assert by_name["deepgram"].ok
    assert by_name["claude"].ok
    assert cost < 0.01
    assert opener.requests[0].full_url.endswith("model=nova-3&language=en")
    assert (
        opener.requests[0].get_header("Authorization") == "Token deepgram-secret-value"
    )
    sent = json.loads(opener.requests[1].data)
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["max_tokens"] == 1
    assert "deepgram-secret-value" not in by_name["deepgram"].detail
    assert "anthropic-secret-value" not in by_name["claude"].detail
    with connect(db_path) as db:
        rows = list(db.execute("SELECT source_id, stage, service FROM api_calls"))
    assert [tuple(row) for row in rows] == [
        (None, "doctor", "deepgram"),
        (None, "doctor", "claude"),
    ]


def test_online_doctor_reports_bad_key_unknown_model_and_quota(db_path: Path) -> None:
    secret = "anthropic-secret-value"
    opener = _Opener(
        [
            _http(401, f"invalid {secret}"),
            _http(404, '{"type":"error","error":{"type":"not_found_error"}}'),
        ]
    )
    results, _cost = doctor.online_checks(_settings(db_path), opener)
    assert [check.detail for check in results] == ["bad key", "unknown model name"]
    assert all(secret not in check.detail for check in results)

    quota = _Opener([_http(402, "credit balance is too low"), _ok_replies()[1]])
    results, _cost = doctor.online_checks(_settings(db_path), quota)
    assert results[0].detail == "no quota"
    assert results[1].ok


def test_online_doctor_does_not_call_when_the_budget_is_zero(db_path: Path) -> None:
    def forbid(request: Request, timeout: float) -> object:
        raise AssertionError("a zero budget must not send a request")

    results, cost = doctor.online_checks(_settings(db_path, run_budget=0), forbid)
    assert cost == 0
    assert len(results) == 2
    assert all("budget" in check.detail for check in results)


def test_online_doctor_blocks_on_a_missing_database(tmp_path: Path) -> None:
    def forbid(request: Request, timeout: float) -> object:
        raise AssertionError("a missing database must not send a request")

    settings = _settings(tmp_path / "missing" / "montaj.db")
    results, cost = doctor.online_checks(settings, forbid)
    assert cost == 0
    assert all("db init" in check.detail for check in results)


def test_doctor_online_command_prints_the_cost(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "deepgram-secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret-value")
    monkeypatch.setattr(doctor.shutil, "which", lambda command: f"/test/{command}")

    def version(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, f"{Path(args[0]).name} 1.2.3\n", "")

    monkeypatch.setattr(doctor.subprocess, "run", version)
    monkeypatch.setattr(doctor, "urlopen", _Opener(_ok_replies()))
    result = CliRunner().invoke(app, ["doctor", "--online"])
    assert result.exit_code == 0, result.output
    assert "OK    deepgram:" in result.output
    assert "OK    claude:" in result.output
    assert "Smoke test: $0.000" in result.output
    assert "secret-value" not in result.output
