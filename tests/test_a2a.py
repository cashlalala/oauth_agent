"""End-to-end protocol tests: real A2AClient -> real FastAPI app (in-process), fake LLM."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from a2a_demo.client.client import A2AClient
from a2a_demo.config import Settings
from a2a_demo.protocol import (
    A2AError,
    ErrorCode,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
)
from a2a_demo.server.app import create_app

SETTINGS = Settings(api_key="x", base_url="http://x", model="fake", host="127.0.0.1", port=9999)


class FakeLLM:
    def __init__(self, chunks=("Hello", " ", "world"), delay=0.0, fail=False):
        self.chunks, self.delay, self.fail = chunks, delay, fail
        self.seen: list[list[dict]] = []

    async def stream(self, messages):
        self.seen.append(messages)
        if self.fail:
            raise RuntimeError("boom")
        for c in self.chunks:
            await asyncio.sleep(self.delay)
            yield c


def run(coro_fn, llm=None):
    """Run `coro_fn(client, llm)` against a fresh in-process server."""

    async def _go():
        llm_ = llm or FakeLLM()
        transport = httpx.ASGITransport(app=create_app(SETTINGS, llm_))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            client = A2AClient("http://test", http_client=http)
            await client.get_agent_card()
            client._rpc_url = "http://test/"
            return await coro_fn(client, llm_)

    return asyncio.run(_go())


def test_agent_card():
    async def go(c, llm):
        assert c.card.name and c.card.capabilities.streaming
        assert c.card.skills[0].id == "chat"

    run(go)


def test_send_blocking_completes_with_artifact():
    async def go(c, llm):
        t = await c.send_message("hi")
        assert isinstance(t, Task)
        assert t.status.state == TaskState.completed
        assert t.artifacts[0].parts[0].text == "Hello world"
        assert [m.role.value for m in t.history] == ["user", "agent"]
        assert t.status.message.text == "Hello world"

    run(go)


def test_context_gives_memory_and_new_task_id():
    async def go(c, llm):
        t1 = await c.send_message("my name is Ada")
        t2 = await c.send_message("who am I?", context_id=t1.contextId)
        assert t2.id != t1.id and t2.contextId == t1.contextId
        sent = llm.seen[1]  # what the 2nd call to the LLM contained
        assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
        assert sent[1]["content"] == "my name is Ada"
        # a different context has no memory
        t3 = await c.send_message("who am I?")
        assert len(llm.seen[2]) == 2

    run(go)


def test_stream_event_order():
    async def go(c, llm):
        events = [e async for e in c.stream_message("hi")]
        assert isinstance(events[0], Task) and events[0].status.state == TaskState.submitted
        states = [e.status.state for e in events if isinstance(e, TaskStatusUpdateEvent)]
        assert states == [TaskState.working, TaskState.completed]
        assert events[-1].final is True
        text = "".join(
            p.text for e in events if isinstance(e, TaskArtifactUpdateEvent) for p in e.artifact.parts
        )
        assert text == "Hello world"
        arts = [e for e in events if isinstance(e, TaskArtifactUpdateEvent)]
        assert arts[0].append is False and arts[1].append is True and arts[-1].lastChunk is True

    run(go)


def test_non_blocking_then_poll():
    async def go(c, llm):
        t = await c.send_message("hi", blocking=False)
        assert t.status.state == TaskState.submitted
        for _ in range(50):
            t = await c.get_task(t.id)
            if t.status.state == TaskState.completed:
                break
            await asyncio.sleep(0.05)
        assert t.status.state == TaskState.completed

    run(go, FakeLLM(delay=0.05))


def test_cancel_running_task():
    async def go(c, llm):
        t = await c.send_message("hi", blocking=False)
        await asyncio.sleep(0.1)
        canceled = await c.cancel_task(t.id)
        assert canceled.status.state == TaskState.canceled
        with pytest.raises(A2AError) as e:
            await c.cancel_task(t.id)
        assert e.value.code == ErrorCode.TASK_NOT_CANCELABLE

    run(go, FakeLLM(chunks=["a"] * 100, delay=0.05))


def test_llm_failure_marks_task_failed():
    async def go(c, llm):
        t = await c.send_message("hi")
        assert t.status.state == TaskState.failed
        assert "boom" in t.status.message.text

    run(go, FakeLLM(fail=True))


def test_errors():
    async def go(c, llm):
        with pytest.raises(A2AError) as e:
            await c.get_task("nope")
        assert e.value.code == ErrorCode.TASK_NOT_FOUND
        with pytest.raises(A2AError) as e:
            await c.rpc("nope/nope", {})
        assert e.value.code == ErrorCode.METHOD_NOT_FOUND
        with pytest.raises(A2AError) as e:
            await c.rpc("message/send", {"message": {"role": "user"}})
        assert e.value.code == ErrorCode.INVALID_PARAMS
        t = await c.send_message("hi")
        with pytest.raises(A2AError) as e:  # terminal tasks accept no more messages
            await c.send_message("again", task_id=t.id)
        assert e.value.code == ErrorCode.INVALID_PARAMS
        # malformed JSON / envelope
        http = c._http
        r = await http.post("http://test/", content=b"{not json")
        assert r.json()["error"]["code"] == ErrorCode.PARSE_ERROR
        r = await http.post("http://test/", json={"hello": 1})
        assert r.json()["error"]["code"] == ErrorCode.INVALID_REQUEST

    run(go)
