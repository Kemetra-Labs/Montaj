"""Small, cost-logged Claude Messages API adapter."""

import json
from typing import Protocol
from urllib.request import Request, urlopen

from montaj.budget import Budget
from montaj.config import Settings
from montaj.costs import Service, Usage
from montaj.external import CallContext, CallResult, call_external


class LLM(Protocol):
    def complete(self, prompt: str) -> str: ...


class Claude:
    def __init__(
        self, settings: Settings, source_id: int, stage: str, budget: Budget
    ) -> None:
        self.settings = settings
        self.source_id = source_id
        self.stage = stage
        self.budget = budget

    def _request(self, prompt: str, key: str, max_tokens: int) -> CallResult[str]:
        body = json.dumps(
            {
                "model": self.settings.claude_model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8")
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
        usage = result["usage"]
        return CallResult(
            text,
            Usage(
                input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"]
            ),
        )

    def complete(self, prompt: str) -> str:
        key = self.settings.anthropic_api_key
        if key is None:
            raise ValueError("Set ANTHROPIC_API_KEY before requesting Claude output.")
        max_tokens = 2200
        # Reserve the output maximum and a conservative input estimate before sending.
        estimated = Usage(
            input_tokens=len(prompt.encode("utf-8")) + 128, output_tokens=max_tokens
        )
        ctx = CallContext(
            self.settings.db_path,
            self.source_id,
            self.stage,
            Service.CLAUDE,
            self.settings.claude_model,
            self.budget,
        )
        return call_external(
            ctx,
            estimated,
            lambda: self._request(prompt, key.get_secret_value(), max_tokens),
            policy=self.settings.retry,
        )
