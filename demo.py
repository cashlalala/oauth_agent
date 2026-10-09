"""Guided tour of the A2A protocol. Start the server first, then:  python demo.py

Each step prints the raw JSON-RPC traffic, so you can see what really goes on the wire.
"""

from __future__ import annotations

import argparse
import asyncio
import json

import httpx

from a2a_demo.client.client import A2AClient
from a2a_demo.config import load_settings
from a2a_demo.protocol import (
    A2AError,
    Task,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
)

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"


def trace(direction: str, payload) -> None:
    print(f"{DIM}{direction} {json.dumps(payload, ensure_ascii=False, indent=2)}{RESET}")


def step(n: int, title: str, why: str) -> None:
    print(f"\n{BOLD}=== Step {n}: {title} ==={RESET}\n{why}\n")


def answer(t: Task) -> str:
    return "".join(p.text for a in (t.artifacts or []) for p in a.parts)


async def main(url: str) -> None:
    async with httpx.AsyncClient(timeout=120) as http:
        c = A2AClient(url, trace=trace, http_client=http)

        step(1, "Discovery", "The client GETs the Agent Card to learn what the agent can do and where to POST.")
        card = await c.get_agent_card()
        print(f"-> {card.name}: skills={[s.id for s in card.skills]} streaming={card.capabilities.streaming}")

        step(2, "message/send (blocking)", "One request, one response. The result is a Task; the reply is its artifact.")
        t1 = await c.send_message("My name is Ada. Reply with just 'Hi Ada'.")
        assert isinstance(t1, Task)
        print(f"-> task={t1.id} state={t1.status.state.value} answer={answer(t1)!r}")

        step(3, "contextId = conversation memory",
             "A NEW task, but we reuse contextId, so the agent remembers task #1. Task ids differ, contextId is shared.")
        t2 = await c.send_message("What is my name?", context_id=t1.contextId)
        assert isinstance(t2, Task)
        print(f"-> new task={t2.id} (same context: {t2.contextId == t1.contextId}) answer={answer(t2)!r}")

        step(4, "message/stream (SSE)",
             "Events: Task(submitted) -> status working -> artifact chunks (append) -> status completed (final=true).")
        c.trace = lambda d, p: None  # too noisy to print every chunk; summarize instead
        n_chunks = 0
        async for ev in c.stream_message("Count from 1 to 5, separated by spaces.", context_id=t1.contextId):
            if isinstance(ev, Task):
                print(f"[task] {ev.id} {ev.status.state.value}")
            elif isinstance(ev, TaskStatusUpdateEvent):
                print(f"\n[status] {ev.status.state.value} final={ev.final}")
            elif isinstance(ev, TaskArtifactUpdateEvent):
                n_chunks += 1
                print("".join(p.text for p in ev.artifact.parts), end="", flush=True)
        print(f"({n_chunks} artifact chunks)")
        c.trace = trace

        step(5, "Non-blocking send + polling",
             "blocking=false returns at once with a 'submitted' task. The client polls tasks/get.")
        c.trace = lambda d, p: None
        t = await c.send_message("Say 'pong'.", blocking=False)
        assert isinstance(t, Task)
        print(f"accepted: {t.id} state={t.status.state.value}")
        while t.status.state.value not in ("completed", "failed", "canceled"):
            await asyncio.sleep(0.5)
            t = await c.get_task(t.id)
            print(f"  poll -> {t.status.state.value}")
        print(f"-> answer={answer(t)!r}")
        c.trace = trace

        step(6, "tasks/cancel", "Start a long streaming task, cancel it from a second request mid-flight.")
        c.trace = lambda d, p: None
        task_id = None
        async for ev in c.stream_message("Write a very long essay (2000 words) about the ocean."):
            if isinstance(ev, Task):
                task_id = ev.id
            elif isinstance(ev, TaskArtifactUpdateEvent):
                print(".", end="", flush=True)
                if task_id:
                    # cancel via a second, independent HTTP request
                    canceled = await c.cancel_task(task_id)
                    print(f"\ncancel -> state={canceled.status.state.value}")
                    task_id = None
            elif isinstance(ev, TaskStatusUpdateEvent) and ev.final:
                print(f"stream ended with final state: {ev.status.state.value}")
        c.trace = trace

        step(7, "Errors", "JSON-RPC errors come back as HTTP 200 with an `error` object.")
        for label, coro in [
            ("tasks/get unknown id (-32001)", c.get_task("does-not-exist")),
            ("tasks/cancel on finished task (-32002)", c.cancel_task(t1.id)),
            ("send to finished task (-32602)", c.send_message("more?", task_id=t1.id)),
            ("unknown method (-32601)", c.rpc("tasks/unknown", {})),
            ("bad params (-32602)", c.rpc("message/send", {"message": {"role": "user"}})),
        ]:
            try:
                await coro
            except A2AError as e:
                print(f"{label}: code={e.code} message={e.message}")

        await c.aclose()


if __name__ == "__main__":
    s = load_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=f"http://{s.host}:{s.port}")
    asyncio.run(main(ap.parse_args().url))
