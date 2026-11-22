"""Offline checks, plus an optional one-request probe of each paid API."""

import json
import shutil
import sqlite3
import subprocess
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from montaj.budget import Budget, BudgetExceeded
from montaj.config import RetrySettings, Settings
from montaj.costs import Service, Usage, estimate_cost
from montaj.db import SCHEMA_VERSION, connect
from montaj.external import CallContext, CallResult, ExternalCallError, call_external
from montaj.housekeeping import disk_free

# Stable yt-dlp release. Nightly builds are not accepted as the pin.
YT_DLP_PIN = "2026.10.26"
YT_DLP_EJS_PIN = "0.8.0"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    warning: bool = False


def _tool(name: str, version_arg: str) -> Check:
    executable = shutil.which(name)
    if executable is None:
        return Check(name, False, f"Install {name} and add it to PATH.")
    try:
        result = subprocess.run(
            [executable, version_arg],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return Check(name, False, f"Check that {name} runs and reports its version.")
    lines = result.stdout.splitlines()
    return Check(name, True, lines[0] if lines else "available")


def _yt_dlp() -> Check:
    check = _tool("yt-dlp", "--version")
    if not check.ok or YT_DLP_PIN in check.detail.split():
        return check
    return Check(
        check.name,
        True,
        f"{check.detail} (pinned {YT_DLP_PIN}; reinstall the pinned release or rebuild the image)",
        warning=True,
    )


def _youtube_dependencies() -> tuple[Check, Check]:
    executable = shutil.which("yt-dlp")
    diagnostics: dict[str, str] = {}
    if executable is not None:
        try:
            # This list command initializes yt-dlp and reports dependencies without a URL.
            result = subprocess.run(
                [
                    executable,
                    "--ignore-config",
                    "--no-plugin-dirs",
                    "--verbose",
                    "--list-impersonate-targets",
                ],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=True,
            )
            for line in result.stderr.splitlines():
                key, _, value = line.partition(": ")
                if key in {"[debug] JS runtimes", "[debug] Optional libraries"}:
                    diagnostics[key] = value
        except (OSError, subprocess.SubprocessError):
            pass
    runtimes = diagnostics.get("[debug] JS runtimes", "").split(", ")
    deno = any(
        runtime.startswith("deno-") and "(" not in runtime for runtime in runtimes
    )
    libraries = diagnostics.get("[debug] Optional libraries", "").split(", ")
    solver = f"yt_dlp_ejs-{YT_DLP_EJS_PIN}" in libraries
    return (
        Check(
            "YouTube runtime",
            True,
            "Deno detected"
            if deno
            else "Supported Deno was not detected. Install Deno or rebuild the Montaj image.",
            warning=not deno,
        ),
        Check(
            "YouTube EJS",
            True,
            f"solver detected ({YT_DLP_EJS_PIN})"
            if solver
            else f"Pinned solver was not detected. Install yt-dlp-ejs {YT_DLP_EJS_PIN} with yt-dlp or rebuild the Montaj image.",
            warning=not solver,
        ),
    )


def _disk(settings: Settings, measure: Callable[[Path], int]) -> Check:
    try:
        free = measure(settings.data_dir)
    except OSError:
        return Check(
            "disk space", False, "Could not read free space on the data volume."
        )
    free_mb = free / (1024 * 1024)
    if free_mb + 1e-8 < settings.min_free_mb:
        return Check(
            "disk space",
            True,
            f"{free_mb:.0f} MB free; warning threshold is {settings.min_free_mb:.0f} MB.",
            warning=True,
        )
    return Check("disk space", True, f"{free_mb:.0f} MB free.")


def _data_directory(path: Path) -> Check:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with TemporaryFile(dir=path) as probe:
            probe.write(b"montaj")
    except OSError:
        return Check(
            "data directory", False, "Set MONTAJ_DATA_DIR to a writable directory."
        )
    return Check("data directory", True, str(path))


def _database(path: Path) -> Check:
    hint = "Run montaj db init to create or migrate the database."
    if not path.is_file():
        return Check("SQLite", False, hint)
    try:
        with connect(path) as db:
            row = db.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
    except sqlite3.Error:
        return Check("SQLite", False, hint)
    if row is None or row[0] != SCHEMA_VERSION:
        found = row[0] if row is not None else "missing"
        return Check(
            "SQLite", False, f"Schema {found}; expected {SCHEMA_VERSION}. {hint}"
        )
    return Check("SQLite", True, f"schema version {SCHEMA_VERSION}")


def checks(
    settings: Settings, *, free_bytes: Callable[[Path], int] | None = None
) -> list[Check]:
    font = settings.resolved_font_path
    font_exists = font.is_file()
    results = [
        _tool("ffmpeg", "-version"),
        _tool("ffprobe", "-version"),
        _yt_dlp(),
        *_youtube_dependencies(),
    ]
    for name, secret in (
        ("DEEPGRAM_API_KEY", settings.deepgram_api_key),
        ("ANTHROPIC_API_KEY", settings.anthropic_api_key),
    ):
        present = bool(secret and secret.get_secret_value().strip())
        results.append(
            Check(
                name,
                present,
                "set" if present else f"missing. Set {name} in the environment.",
            )
        )
    results.extend(
        (
            _data_directory(settings.data_dir),
            _database(settings.db_path),
            Check(
                "Arabic font",
                font_exists,
                str(font)
                if font_exists
                else "Set MONTAJ_FONT_PATH to an Arabic-capable font file.",
            ),
        )
    )
    results.append(_disk(settings, free_bytes or disk_free))
    return results


# One cent. The smoke test refuses to send a request priced at or above this.
_CENT = 0.01
_Opener = Callable[..., Any]


def _redact(text: str, settings: Settings) -> str:
    for secret in (settings.deepgram_api_key, settings.anthropic_api_key):
        if secret is None:
            continue
        raw = secret.get_secret_value()
        if raw:
            text = text.replace(raw, "[redacted]")
    return text


def _service_reason(exc: Exception) -> str:
    if isinstance(exc, BudgetExceeded):
        return str(exc)
    folded = str(exc).casefold()
    if "unknown model" in folded:
        return "unknown model name"
    if any(phrase in folded for phrase in ("quota", "billing", "http 402", "http 429")):
        return "no quota"
    if any(phrase in folded for phrase in ("authentication", "http 401", "http 403")):
        return "bad key"
    return str(exc)


def _silent_wav(path: Path) -> None:
    """One second of silence, the smallest clip that still exercises listen."""
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 8000)


def _probe(
    settings: Settings,
    budget: Budget,
    name: str,
    service: Service,
    model: str,
    usage: Usage,
    send: Callable[[], CallResult[None]],
) -> Check:
    try:
        estimate = estimate_cost(service, model, usage)
    except ValueError as exc:
        return Check(name, False, _redact(str(exc), settings))
    if estimate >= _CENT:
        return Check(name, False, "smoke test would cost more than one cent")
    context = CallContext(settings.db_path, None, "doctor", service, model, budget)
    try:
        call_external(context, usage, send, policy=RetrySettings(attempts=1))
    except (ExternalCallError, BudgetExceeded, ValueError) as exc:
        return Check(name, False, _redact(_service_reason(exc), settings))
    return Check(name, True, model)


def _deepgram_send(
    path: Path, key: str, model: str, language: str, opener: _Opener
) -> Callable[[], CallResult[None]]:
    def send() -> CallResult[None]:
        query = urlencode({"model": model, "language": language})
        request = Request(
            f"https://api.deepgram.com/v1/listen?{query}",
            data=path.read_bytes(),
            method="POST",
            headers={"Content-Type": "audio/wav"},
        )
        request.add_unredirected_header("Authorization", f"Token {key}")
        with opener(request, timeout=30) as response:
            payload = json.load(response)
        seconds = 1.0
        if isinstance(payload, dict):
            meta = payload.get("metadata")
            if isinstance(meta, dict) and isinstance(meta.get("duration"), int | float):
                seconds = float(meta["duration"])
        return CallResult(None, Usage(seconds=seconds))

    return send


def _claude_send(
    model: str, key: str, opener: _Opener
) -> Callable[[], CallResult[None]]:
    def send() -> CallResult[None]:
        # between_tools skips up-front thinking so this stays a one-token reply.
        # https://platform.claude.com/docs/en/models/sonnet-5-5/whats-new-sonnet-5-5
        body = json.dumps(
            {
                "model": model,
                "max_tokens": 1,
                "thinking": {"type": "between_tools"},
                "messages": [{"role": "user", "content": "Reply with ok."}],
            }
        ).encode()
        request = Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            },
        )
        request.add_unredirected_header("X-Api-Key", key)
        with opener(request, timeout=30) as response:
            payload = json.load(response)
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            usage = {}
        return CallResult(
            None,
            Usage(
                input_tokens=int(usage.get("input_tokens") or 1),
                output_tokens=int(usage.get("output_tokens") or 1),
            ),
        )

    return send


def _smoke_cost(settings: Settings, run_id: str) -> float:
    with connect(settings.db_path) as db:
        row = db.execute(
            "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM api_calls WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    return float(row[0])


def online_checks(
    settings: Settings, opener: _Opener | None = None
) -> tuple[list[Check], float]:
    """Probe Deepgram and Claude once each. Return checks and the logged cost."""
    transport = urlopen if opener is None else opener
    ready = _database(settings.db_path)
    if not ready.ok:
        blocked = Check("deepgram", False, ready.detail)
        return [blocked, Check("claude", False, ready.detail)], 0.0
    budget = Budget.from_settings(settings)
    results: list[Check] = []
    deepgram_key = settings.deepgram_api_key
    if deepgram_key is None or not deepgram_key.get_secret_value().strip():
        results.append(Check("deepgram", False, "bad key"))
    else:
        billing = settings.deepgram_model
        if settings.deepgram_language == "multi":
            billing = f"{billing}-multilingual"
        with TemporaryDirectory() as directory:
            audio = Path(directory) / "silence.wav"
            _silent_wav(audio)
            results.append(
                _probe(
                    settings,
                    budget,
                    "deepgram",
                    Service.DEEPGRAM,
                    billing,
                    Usage(seconds=1),
                    _deepgram_send(
                        audio,
                        deepgram_key.get_secret_value(),
                        settings.deepgram_model,
                        settings.deepgram_language,
                        transport,
                    ),
                )
            )
    claude_key = settings.anthropic_api_key
    if claude_key is None or not claude_key.get_secret_value().strip():
        results.append(Check("claude", False, "bad key"))
    else:
        results.append(
            _probe(
                settings,
                budget,
                "claude",
                Service.CLAUDE,
                settings.claude_model,
                Usage(input_tokens=16, output_tokens=1),
                _claude_send(
                    settings.claude_model, claude_key.get_secret_value(), transport
                ),
            )
        )
    cost = _smoke_cost(settings, budget.run_id)
    if cost >= _CENT:
        results.append(Check("smoke test", False, f"${cost:.6f} is more than one cent"))
    return results, cost
