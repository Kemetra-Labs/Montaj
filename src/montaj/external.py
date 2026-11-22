"""Retry paid provider calls and record every attempt without exposing secrets."""

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError

from montaj.budget import Budget, check_budget
from montaj.config import RetrySettings, Settings
from montaj.costs import Service, Usage, estimate_cost, log_api_call
from montaj.db import connect
from montaj.models import utc_now


class ExternalCallError(RuntimeError):
    """Safe, user-facing error from a paid service call."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class CallContext:
    db_path: Path
    source_id: int
    stage: str
    service: Service
    model: str
    budget: Budget = field(
        default_factory=lambda: Budget.from_settings(Settings.from_env())
    )
    nonretryable_http: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class CallResult[T]:
    value: T
    usage: Usage


@dataclass(frozen=True, slots=True)
class _Failure:
    reason: str
    retry: bool = False
    retry_after: str | None = None
    may_bill: bool = False
    http_status: int | None = None


def _http_failure(error: HTTPError) -> _Failure:
    status = error.code
    retry_after = error.headers.get("Retry-After") if error.headers else None
    try:
        payload = error.read(8192).decode("utf-8", errors="replace").casefold()
    except OSError:
        payload = ""
    finally:
        error.close()
    billing_phrases = (
        "insufficient_credits",
        "insufficient_quota",
        "credit balance is too low",
        "billing_error",
        "billing hard limit",
        "quota_exceeded",
    )
    if status == 402 or any(phrase in payload for phrase in billing_phrases):
        return _Failure("quota or billing limit reached")
    if status in (401, 403):
        return _Failure(f"authentication or permissions failed (HTTP {status})")
    if status == 429 or 500 <= status < 600:
        return _Failure(
            f"temporary HTTP {status}", True, retry_after, status >= 500, status
        )
    return _Failure(f"request failed (HTTP {status})")


def _classify(error: Exception) -> _Failure:
    if isinstance(error, HTTPError):
        return _http_failure(error)
    if isinstance(error, (TimeoutError, URLError, ConnectionError)):
        return _Failure("temporary connection failure", True, may_bill=True)
    if isinstance(error, (json.JSONDecodeError, UnicodeError)):
        return _Failure("provider returned invalid JSON", may_bill=True)
    return _Failure("unexpected provider response", may_bill=True)


def _delay(
    retry_after: str | None,
    now: Callable[[], datetime],
    fallback: float,
) -> float:
    if retry_after:
        try:
            seconds = int(retry_after)
        except ValueError:
            try:
                seconds = int(
                    (parsedate_to_datetime(retry_after) - now()).total_seconds()
                )
            except (TypeError, ValueError, OverflowError):
                return fallback
        return max(0, seconds)
    return fallback


def _reserve(context: CallContext, usage: Usage, estimate: float) -> int:
    with connect(context.db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        check_budget(db, context.budget, context.source_id, context.stage, estimate)
        return log_api_call(
            db,
            context.source_id,
            context.stage,
            context.service,
            usage,
            estimate,
            reservation=context.budget.run_id,
        )


def _settle(
    context: CallContext, call_id: int, usage: Usage, cost: float, outcome: str
) -> None:
    with connect(context.db_path) as db:
        db.execute(
            """UPDATE api_calls SET input_tokens = ?, output_tokens = ?, seconds = ?,
               estimated_cost_usd = ?, outcome = ? WHERE id = ?""",
            (
                usage.input_tokens,
                usage.output_tokens,
                usage.seconds,
                cost,
                outcome,
                call_id,
            ),
        )


def call_external[T](
    context: CallContext,
    estimated_usage: Usage,
    fn: Callable[[], CallResult[T]],
    *,
    policy: RetrySettings | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = utc_now,
    jitter: Callable[[float, float], float] = random.uniform,
) -> T:
    """Invoke a provider, logging each attempt in its own transaction."""
    settings = policy or RetrySettings()
    estimate = estimate_cost(context.service, context.model, estimated_usage)
    for attempt in range(settings.attempts):
        call_id = _reserve(context, estimated_usage, estimate)
        try:
            result = fn()
        except Exception as exc:
            failure = _classify(exc)
        else:
            _settle(
                context,
                call_id,
                result.usage,
                estimate_cost(context.service, context.model, result.usage),
                "success",
            )
            return result.value

        _settle(
            context,
            call_id,
            estimated_usage if failure.may_bill else Usage(),
            estimate if failure.may_bill else 0.0,
            "failure",
        )
        if (
            not failure.retry
            or failure.http_status in context.nonretryable_http
            or attempt + 1 == settings.attempts
        ):
            raise ExternalCallError(
                f"{context.service} {context.stage}: {failure.reason}.",
                http_status=failure.http_status,
            )
        backoff = min(settings.max_delay, settings.base_delay * 2**attempt)
        sleep(
            _delay(
                failure.retry_after,
                now,
                min(settings.max_delay, backoff + jitter(0, 0.25)),
            )
        )
    raise AssertionError("Retry policy must allow at least one attempt.")
