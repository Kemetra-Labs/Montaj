"""Small, cost-logged Claude Messages API adapter."""

import json
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.request import Request, urlopen

from montaj.budget import Budget
from montaj.config import Settings
from montaj.costs import Service, Usage
from montaj.external import CallContext, CallResult, ExternalCallError, call_external


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
        response = call_external(
            ctx,
            estimated,
            lambda: self._request(prompt, key.get_secret_value()),
            policy=self.settings.retry,
        )
        # HTTP success still incurs usage. Check completion only after settling the call.
        if self.require_complete and response.stop_reason != "end_turn":
            if response.stop_reason == "max_tokens":
                detail = (
                    f"output truncated at {response.output_tokens} tokens "
                    f"(limit {self.max_tokens}). Increase ranking output headroom or reduce eligible candidates before retrying."
                )
            elif response.stop_reason == "model_context_window_exceeded":
                detail = "output truncated at the model context limit. Reduce eligible candidates before retrying."
            elif response.stop_reason == "refusal":
                detail = "Claude declined to return grades. Review the source and ranking prompt before retrying."
            else:
                detail = "Claude did not confirm a complete response. Check the model response format before retrying."
            raise ExternalCallError(f"claude {self.stage}: {detail}")
        return response.text
