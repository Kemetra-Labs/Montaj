from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

from montaj.costs import Service, Usage
from montaj.db import connect, insert
from montaj.external import CallContext, CallResult, ExternalCallError, call_external
from montaj.models import Niche, Source
from tests.fakes import FakeClock, FakeTransport


@pytest.fixture
def context(db_path: Path) -> CallContext:
    with connect(db_path) as db:
        source = insert(db, Source(path="audio.wav", title="Test", niche=Niche.GAMES))
        assert source.id is not None
    return CallContext(db_path, source.id, "transcribe", Service.DEEPGRAM, "nova-3")


def http_error(
    status: int, body: str = "", retry_after: str | None = None
) -> HTTPError:
    headers = Message()
    if retry_after:
        headers["Retry-After"] = retry_after
    return HTTPError(
        "https://example.test", status, "test-secret", headers, BytesIO(body.encode())
    )


def test_rate_limit_honors_retry_after_and_logs_both_attempts(
    context: CallContext,
) -> None:
    usage = Usage(seconds=60)
    response = CallResult("done", usage)
    transport = FakeTransport([http_error(429, retry_after="3"), response])
    clock = FakeClock()

    result = call_external(
        context,
        usage,
        lambda: transport.send("request"),
        sleep=clock.sleep,
        now=clock.now,
    )

    assert result == "done"
    assert clock.delays == [3]
    with connect(context.db_path) as db:
        attempts = db.execute(
            "SELECT seconds, estimated_cost_usd FROM api_calls ORDER BY id"
        )
        failed, success = map(tuple, attempts)
        assert failed == (None, 0.0)
        assert success[0] == 60 and success[1] > 0


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (401, "invalid test-secret", "authentication"),
        (400, "credit balance is too low, test-secret", "quota or billing"),
    ],
)
def test_permanent_errors_stop_without_leaking_secrets(
    context: CallContext,
    status: int,
    body: str,
    reason: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = FakeTransport(
        [http_error(status, body), CallResult("unexpected", Usage())]
    )
    clock = FakeClock()
    with pytest.raises(ExternalCallError, match=reason) as raised:
        call_external(
            context,
            Usage(seconds=10),
            lambda: transport.send("request"),
            sleep=clock.sleep,
        )
    assert "test-secret" not in str(raised.value)
    assert "test-secret" not in capsys.readouterr().err
    assert clock.delays == []
    assert transport.requests == ["request"]
    with connect(context.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0] == 1
