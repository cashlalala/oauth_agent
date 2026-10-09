"""A minimal A2A client: discovery + JSON-RPC calls + SSE streaming, over httpx."""

from __future__ import annotations

import itertools
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from ..protocol import (
    AGENT_CARD_PATH,
    A2AError,
    AgentCard,
    ErrorCode,
    Message,
    Role,
    StreamEvent,
    Task,
    parse_result,
    rpc_request,
    text_message,
)

# trace(direction, payload): lets a UI show exactly what is on the wire
TraceFn = Callable[[str, Any], None]


class A2AClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 120.0,
        trace: TraceFn | None = None,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.trace = trace or (lambda direction, payload: None)
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._ids = itertools.count(1)
        self.card: AgentCard | None = None
        self._rpc_url = self.base_url + "/"

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ #
    # discovery
    # ------------------------------------------------------------------ #
    async def get_agent_card(self) -> AgentCard:
        url = self.base_url + AGENT_CARD_PATH
        self.trace("GET", url)
        resp = await self._http.get(url)
        resp.raise_for_status()
        self.trace("GET <-", resp.json())
        self.card = AgentCard.model_validate(resp.json())
        self._rpc_url = self.card.url  # the card is the source of truth for the endpoint
        return self.card

    # ------------------------------------------------------------------ #
    # high-level API
    # ------------------------------------------------------------------ #
    def _send_params(
        self, text: str, context_id: str | None, task_id: str | None, blocking: bool | None
    ) -> dict[str, Any]:
        msg = text_message(Role.user, text, contextId=context_id, taskId=task_id)
        params: dict[str, Any] = {"message": msg.to_wire()}
        if blocking is not None:
            params["configuration"] = {"blocking": blocking}
        return params

    async def send_message(
        self,
        text: str,
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        blocking: bool = True,
    ) -> Task | Message:
        """message/send. With blocking=False the returned Task is usually still
        'submitted'; poll get_task() until it reaches a terminal state."""
        result = await self.rpc("message/send", self._send_params(text, context_id, task_id, blocking))
        return parse_result(result)  # type: ignore[return-value]

    async def stream_message(
        self, text: str, *, context_id: str | None = None
    ) -> AsyncIterator[StreamEvent]:
        """message/stream: yields Task, then status/artifact update events."""
        async for result in self.rpc_stream("message/stream", self._send_params(text, context_id, None, None)):
            yield parse_result(result)

    async def get_task(self, task_id: str, history_length: int | None = None) -> Task:
        params: dict[str, Any] = {"id": task_id}
        if history_length is not None:
            params["historyLength"] = history_length
        return Task.model_validate(await self.rpc("tasks/get", params))

    async def cancel_task(self, task_id: str) -> Task:
        return Task.model_validate(await self.rpc("tasks/cancel", {"id": task_id}))

    # ------------------------------------------------------------------ #
    # JSON-RPC plumbing
    # ------------------------------------------------------------------ #
    async def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """One request -> one response. Raises A2AError for JSON-RPC errors."""
        payload = rpc_request(method, params, next(self._ids))
        self.trace("->", payload)
        resp = await self._http.post(self._rpc_url, json=payload)
        resp.raise_for_status()
        body = resp.json()
        self.trace("<-", body)
        return self._unwrap(body)

    async def rpc_stream(self, method: str, params: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """One request -> many responses, delivered as Server-Sent Events."""
        payload = rpc_request(method, params, next(self._ids))
        self.trace("->", payload)
        async with self._http.stream(
            "POST", self._rpc_url, json=payload, headers={"Accept": "text/event-stream"}
        ) as resp:
            resp.raise_for_status()
            if "text/event-stream" not in resp.headers.get("content-type", ""):
                # the server rejected the call before streaming: plain JSON-RPC error
                body = json.loads(await resp.aread())
                self.trace("<-", body)
                yield self._unwrap(body)
                return
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):  # skip blank separators / comments
                    continue
                body = json.loads(line[len("data:"):].strip())
                self.trace("<- SSE", body)
                yield self._unwrap(body)

    @staticmethod
    def _unwrap(body: dict[str, Any]) -> dict[str, Any]:
        if "error" in body:
            e = body["error"]
            raise A2AError(e.get("code", ErrorCode.INTERNAL_ERROR), e.get("message", ""), e.get("data"))
        return body["result"]
