"""Interactive chat CLI on top of A2AClient.  Run: python -m a2a_demo.client"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import httpx

from ..config import load_settings
from ..protocol import (
    A2AError,
    Message,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
    TERMINAL_STATES,
)
from .client import A2AClient

DIM, CYAN, YELLOW, RED, RESET = "\033[2m", "\033[36m", "\033[33m", "\033[31m", "\033[0m"

HELP = """commands:
  /card            show the agent card
  /stream          toggle streaming (message/stream) vs blocking (message/send)
  /async <text>    send non-blocking, then poll tasks/get until finished
  /task [id]       tasks/get for the last (or given) task
  /cancel [id]     tasks/cancel for the last (or given) task
  /trace           toggle raw JSON-RPC wire trace
  /new             start a new conversation (new contextId)
  /quit            exit
anything else is sent to the agent as a chat message."""


def make_trace(enabled: list[bool]):
    def trace(direction: str, payload: Any) -> None:
        if enabled[0]:
            print(f"{DIM}{direction} {json.dumps(payload, ensure_ascii=False, indent=2)}{RESET}")

    return trace


def reply_text(task: Task) -> str:
    if task.artifacts:
        return "".join(p.text for a in task.artifacts for p in a.parts)
    return task.status.message.text if task.status.message else ""


async def chat(args: argparse.Namespace) -> None:
    tracing = [args.trace]
    streaming = [not args.no_stream]
    context_id: str | None = None
    last_task_id: str | None = None

    async with httpx.AsyncClient(timeout=120) as http:
        client = A2AClient(args.url, trace=make_trace(tracing), http_client=http)
        try:
            card = await client.get_agent_card()
        except httpx.HTTPError as e:
            print(f"{RED}cannot reach agent at {args.url}: {e}{RESET}\nIs the server running?")
            return
        print(f"connected to {CYAN}{card.name}{RESET} v{card.version} (streaming={card.capabilities.streaming})")
        print(HELP)

        while True:
            try:
                line = (await asyncio.to_thread(input, f"\n{YELLOW}you>{RESET} ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            cmd, _, rest = line.partition(" ")
            rest = rest.strip()

            try:
                if cmd in ("/quit", "/exit"):
                    break
                elif cmd == "/help":
                    print(HELP)
                elif cmd == "/card":
                    print(json.dumps(card.to_wire(), indent=2, ensure_ascii=False))
                elif cmd == "/trace":
                    tracing[0] = not tracing[0]
                    print(f"trace {'on' if tracing[0] else 'off'}")
                elif cmd == "/stream":
                    streaming[0] = not streaming[0]
                    print(f"streaming {'on' if streaming[0] else 'off'}")
                elif cmd == "/new":
                    context_id = None
                    print("new conversation")
                elif cmd == "/task":
                    t = await client.get_task(rest or _need(last_task_id))
                    print(f"{t.id}: {t.status.state.value}, history={len(t.history)} msgs")
                elif cmd == "/cancel":
                    t = await client.cancel_task(rest or _need(last_task_id))
                    print(f"{t.id}: {t.status.state.value}")
                elif cmd == "/async":
                    t = await client.send_message(rest, context_id=context_id, blocking=False)
                    assert isinstance(t, Task)
                    context_id, last_task_id = t.contextId, t.id
                    print(f"{DIM}task {t.id} accepted, state={t.status.state.value}; polling...{RESET}")
                    while t.status.state not in TERMINAL_STATES:
                        await asyncio.sleep(0.5)
                        t = await client.get_task(t.id)
                        print(f"{DIM}  poll -> {t.status.state.value}{RESET}")
                    print(f"{CYAN}agent>{RESET} {reply_text(t)}")
                elif cmd.startswith("/"):
                    print("unknown command, try /help")
                elif streaming[0]:
                    context_id, last_task_id = await _stream_turn(client, line, context_id)
                else:
                    t = await client.send_message(line, context_id=context_id)
                    if isinstance(t, Task):
                        context_id, last_task_id = t.contextId, t.id
                        _print_final(t)
                    else:  # agents may answer with a bare Message instead of a Task
                        print(f"{CYAN}agent>{RESET} {t.text}")
            except A2AError as e:
                print(f"{RED}A2A error {e.code}: {e.message}{RESET}")
            except httpx.HTTPError as e:
                print(f"{RED}HTTP error: {e}{RESET}")

        await client.aclose()


def _need(task_id: str | None) -> str:
    if not task_id:
        raise A2AError(-32602, "no task yet; pass a task id")
    return task_id


def _print_final(t: Task) -> None:
    if t.status.state == TaskState.completed:
        print(f"{CYAN}agent>{RESET} {reply_text(t)}")
    else:
        print(f"{RED}task {t.status.state.value}: {t.status.message.text if t.status.message else ''}{RESET}")


async def _stream_turn(client: A2AClient, text: str, context_id: str | None) -> tuple[str | None, str | None]:
    task_id = None
    print(f"{CYAN}agent>{RESET} ", end="", flush=True)
    async for ev in client.stream_message(text, context_id=context_id):
        if isinstance(ev, Task):
            context_id, task_id = ev.contextId, ev.id
        elif isinstance(ev, TaskArtifactUpdateEvent):
            print("".join(p.text for p in ev.artifact.parts), end="", flush=True)
        elif isinstance(ev, TaskStatusUpdateEvent):
            if ev.status.state == TaskState.failed:
                print(f"\n{RED}task failed: {ev.status.message.text if ev.status.message else ''}{RESET}", end="")
            elif ev.status.state == TaskState.canceled:
                print(f"\n{RED}[canceled]{RESET}", end="")
        elif isinstance(ev, Message):
            print(ev.text, end="")
    print()
    return context_id, task_id


def main() -> None:
    s = load_settings()
    p = argparse.ArgumentParser(description="A2A chat client")
    p.add_argument("--url", default=f"http://{s.host}:{s.port}", help="base URL of the A2A server")
    p.add_argument("--no-stream", action="store_true", help="use message/send instead of message/stream")
    p.add_argument("--trace", action="store_true", help="print raw JSON-RPC traffic")
    asyncio.run(chat(p.parse_args()))


if __name__ == "__main__":
    main()
