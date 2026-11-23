"""Small, cost-logged Claude Messages API adapter."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.request import Request, urlopen

from montaj.budget import Budget
from montaj.config import Settings
from montaj.costs import Service, Usage
from montaj.external import (
    CallContext,
    CallResult,
    ExternalCallError,
    RejectedResponse,
    call_external,
)


class LLM(Protocol):
    def complete(self, prompt: str) -> str: ...


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    stop_reason: str | None
    output_tokens: int


class Claude:
    def __init__(
        self,
        settings: Settings,
        source_id: int,
        stage: str,
        budget: Budget,
        *,
        max_tokens: int = 2200,
        require_complete: bool = False,
        thinking: Literal["between_tools"] | None = None,
    ) -> None:
        self.settings = settings
        self.source_id = source_id
        self.stage = stage
        self.budget = budget
        self.max_tokens = max_tokens
        self.require_complete = require_complete
        self.thinking = thinking

    def _request(self, prompt: str, key: str) -> CallResult[Completion]:
        payload: dict[str, object] = {
            "model": self.settings.claude_model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.thinking is not None:
            payload["thinking"] = {"type": self.thinking}
        body = json.dumps(payload).encode("utf-8")
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
        with urlopen(request, timeout=120) as response:
            result = json.load(response)
        text = "\n".join(
            item["text"] for item in result["content"] if item["type"] == "text"
        )
        usage = Usage(
            input_tokens=result["usage"]["input_tokens"],
            output_tokens=result["usage"]["output_tokens"],
        )
        return CallResult(
            Completion(text, result.get("stop_reason"), usage.output_tokens or 0), usage
        )

    def complete(self, prompt: str) -> str:
        return self._complete(prompt, str)

    def _complete[T](self, prompt: str, decode: Callable[[str], T]) -> T:
        key = self.settings.anthropic_api_key
        if key is None:
            raise ValueError("Set ANTHROPIC_API_KEY before requesting Claude output.")
        # Reserve the output maximum and a conservative input estimate before sending.
        estimated = Usage(
            input_tokens=len(prompt.encode("utf-8")) + 128,
            output_tokens=self.max_tokens,
        )
        ctx = CallContext(
            self.settings.db_path,
            self.source_id,
            self.stage,
            Service.CLAUDE,
            self.settings.claude_model,
            self.budget,
        )

        def request() -> CallResult[T]:
            result = self._request(prompt, key.get_secret_value())
            try:
                self._check_completion(result.value)
                value = decode(result.value.text)
            except (ValueError, ExternalCallError) as exc:
                raise RejectedResponse(result.usage, exc) from None
            return CallResult(value, result.usage)

        return call_external(
            ctx,
            estimated,
            request,
            policy=self.settings.retry,
        )

    def _check_completion(self, response: Completion) -> None:
        reason = response.stop_reason
        if reason == "max_tokens":
            detail = (
                f"output truncated at {response.output_tokens} tokens "
                f"(limit {self.max_tokens}). Review output headroom before retrying."
            )
        elif reason == "model_context_window_exceeded":
            detail = "output truncated at the model context limit. Review request size before retrying."
        elif reason == "refusal":
            detail = "Claude declined to complete the request. Review the source and prompt before retrying."
        elif self.require_complete and reason != "end_turn":
            detail = "Claude did not confirm a complete response. Check the model response format before retrying."
        else:
            return
        raise ExternalCallError(f"claude {self.stage}: {detail}")


def complete_checked[T](client: LLM, prompt: str, decode: Callable[[str], T]) -> T:
    """Validate model output while the production adapter still has its usage."""
    if isinstance(client, Claude):
        return client._complete(prompt, decode)
    return decode(client.complete(prompt))
