"""The "brain" of the agent: an OpenAI-compatible client pointed at OpenRouter.

The A2A layer knows nothing about OpenAI. It only needs an object with
    async def stream(messages) -> AsyncIterator[str]
so tests can swap in a fake.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from openai import AsyncOpenAI

from ..config import Settings

SYSTEM_PROMPT = (
    "You are a friendly, concise chat assistant exposed as an A2A (Agent2Agent) agent. "
    "Answer clearly. Keep replies short unless the user asks for detail."
)


class ChatBackend(Protocol):
    def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]: ...


class OpenRouterChat:
    def __init__(self, settings: Settings):
        if not settings.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set (see .env.example)")
        self._model = settings.model
        self._client = AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            # optional OpenRouter attribution headers
            default_headers={"HTTP-Referer": "http://localhost", "X-Title": "a2a-demo"},
        )

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        resp = await self._client.chat.completions.create(
            model=self._model, messages=messages, stream=True
        )
        async for chunk in resp:
            if not chunk.choices:  # e.g. usage-only chunk
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
