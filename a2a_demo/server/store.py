"""In-memory state: tasks, per-context chat history, running jobs and the event bus."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from ..protocol import StreamEvent, Task


class EventBus:
    """Fan-out of task events to however many listeners are attached.

    The agent's worker publishes events as it makes progress. A `message/stream`
    request, or a blocking `message/send` request, subscribes and consumes them.
    A non-blocking send has no subscriber: the work just continues and the client
    polls `tasks/get`.
    """

    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue[StreamEvent]]] = defaultdict(list)

    def subscribe(self, task_id: str) -> asyncio.Queue[StreamEvent]:
        q: asyncio.Queue[StreamEvent] = asyncio.Queue()
        self._subs[task_id].append(q)
        return q

    def unsubscribe(self, task_id: str, q: asyncio.Queue[StreamEvent]) -> None:
        if q in self._subs.get(task_id, []):
            self._subs[task_id].remove(q)
        if not self._subs.get(task_id):
            self._subs.pop(task_id, None)

    def publish(self, task_id: str, event: StreamEvent) -> None:
        for q in list(self._subs.get(task_id, [])):
            q.put_nowait(event)


class TaskStore:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        # contextId -> OpenAI-style messages [{"role": "user"|"assistant", "content": ...}]
        # This is what gives the agent memory across several tasks of one conversation.
        self.contexts: dict[str, list[dict[str, str]]] = defaultdict(list)
        # taskId -> the asyncio job doing the work (needed for tasks/cancel)
        self.jobs: dict[str, asyncio.Task[None]] = {}

    def get(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def save(self, task: Task) -> None:
        self.tasks[task.id] = task
