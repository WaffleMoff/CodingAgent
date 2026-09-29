from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from config import API_KEY, BASE_URL, MODEL


@dataclass
class Completion:
    """Normalized view of a chat completion.

    `finish_reason` is surfaced because it is the only reliable signal that the
    model was cut off by the output cap rather than emitting bad JSON. Without
    it, a truncated tool call is indistinguishable from a malformed one and the
    agent cannot choose a different strategy.
    """

    content: str
    tool_calls: list[Any]
    finish_reason: str | None = None

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


class LLM:
    """Thin wrapper over the chat-completions API shared by every agent."""

    def __init__(self, model: str = MODEL, max_tokens: int | None = None):
        self.model = model
        self.client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
        self.max_tokens = max_tokens or int(
            os.getenv("MAX_COMPLETION_TOKENS", "32768")
        )

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Completion:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools or None,
            tool_choice="auto" if tools else None,
            max_tokens=self.max_tokens,
        )
        choice = response.choices[0]
        message = choice.message
        return Completion(
            content=message.content or "",
            tool_calls=list(message.tool_calls or []),
            finish_reason=choice.finish_reason,
        )
