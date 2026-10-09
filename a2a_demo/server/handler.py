"""The A2A request handler: implements the protocol methods on top of the chat backend.

Task lifecycle implemented here:

    submitted --> working --> completed
                     |------> failed      (LLM/network error)
                     '------> canceled    (tasks/cancel)

Execution model
---------------
Every incoming message starts a *job* (an asyncio task) that talks to the LLM and
publishes events to the EventBus. The three ways of consuming that job are:

  message/send  blocking=true   subscribe, wait for the final event, return the Task
  message/send  blocking=false  don't subscribe, return the (submitted) Task at once
  message/stream                subscribe, forward every event as Server-Sent Events
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from ..protocol import (
    A2AError,
    Artifact,
    ErrorCode,
    Message,
    MessageSendParams,
    Role,
    StreamEvent,
    Task,
    TaskArtifactUpdateEvent,
    TaskIdParams,
    TaskQueryParams,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TERMINAL_STATES,
    TextPart,
    new_id,
    text_message,
)
from .llm import SYSTEM_PROMPT, ChatBackend
from .store import EventBus, TaskStore

log = logging.getLogger("a2a.server")


class A2AHandler:
    def __init__(self, llm: ChatBackend):
        self.llm = llm
        self.store = TaskStore()
        self.bus = EventBus()

    # ------------------------------------------------------------------ #
    # message/send
    # ------------------------------------------------------------------ #
    async def message_send(self, params: MessageSendParams) -> Task:
        task, queue = self._start(params)
        blocking = True if params.configuration is None else params.configuration.blocking is not False
        if not blocking:
            self.bus.unsubscribe(task.id, queue)
            return self._snapshot(task.id, params)  # still "submitted"
        try:
            while True:
                event = await queue.get()
                if getattr(event, "final", False):
                    break
        finally:
            self.bus.unsubscribe(task.id, queue)
        return self._snapshot(task.id, params)

    # ------------------------------------------------------------------ #
    # message/stream
    # ------------------------------------------------------------------ #
    async def message_stream(self, params: MessageSendParams) -> tuple[Task, AsyncIterator[StreamEvent]]:
        """Validates + starts the job *before* the HTTP response is opened, so that
        protocol errors can still be returned as a normal JSON-RPC error."""
        task, queue = self._start(params)
        first = self._snapshot(task.id, params)

        async def events() -> AsyncIterator[StreamEvent]:
            try:
                while True:
                    event = await queue.get()
                    yield event
                    if getattr(event, "final", False):
                        return
            finally:
                self.bus.unsubscribe(task.id, queue)

        return first, events()

    # ------------------------------------------------------------------ #
    # tasks/get, tasks/cancel
    # ------------------------------------------------------------------ #
    async def tasks_get(self, params: TaskQueryParams) -> Task:
        if self.store.get(params.id) is None:
            raise A2AError(ErrorCode.TASK_NOT_FOUND, f"Task not found: {params.id}")
        return self._snapshot(params.id, history_length=params.historyLength)

    async def tasks_cancel(self, params: TaskIdParams) -> Task:
        task = self.store.get(params.id)
        if task is None:
            raise A2AError(ErrorCode.TASK_NOT_FOUND, f"Task not found: {params.id}")
        if task.status.state in TERMINAL_STATES:
            raise A2AError(
                ErrorCode.TASK_NOT_CANCELABLE,
                f"Task {params.id} is already {task.status.state.value} and cannot be canceled",
            )
        job = self.store.jobs.get(params.id)
        if job:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)  # _run() records "canceled"
        return self._snapshot(params.id)

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _start(self, params: MessageSendParams) -> tuple[Task, asyncio.Queue[StreamEvent]]:
        msg = params.message
        if msg.role != Role.user:
            raise A2AError(ErrorCode.INVALID_PARAMS, "message.role must be 'user'")
        if not msg.text.strip():
            raise A2AError(ErrorCode.INVALID_PARAMS, "message must contain some text")

        if msg.taskId:
            # Spec: a message may continue an existing task, but only a task that is
            # waiting for input. Our agent never asks for input, so every existing
            # task is either running or in a terminal state.
            existing = self.store.get(msg.taskId)
            if existing is None:
                raise A2AError(ErrorCode.TASK_NOT_FOUND, f"Task not found: {msg.taskId}")
            raise A2AError(
                ErrorCode.INVALID_PARAMS,
                f"Task {msg.taskId} is '{existing.status.state.value}' and cannot accept new "
                "messages. Omit taskId (and reuse contextId) to start a follow-up task.",
            )

        context_id = msg.contextId or new_id()
        task = Task(
            contextId=context_id,
            status=TaskStatus(state=TaskState.submitted),
            history=[],
        )
        msg.taskId, msg.contextId = task.id, context_id
        task.history.append(msg)
        self.store.save(task)

        queue = self.bus.subscribe(task.id)  # subscribe BEFORE the job starts: no missed events
        self.store.jobs[task.id] = asyncio.create_task(self._run(task.id, msg))
        log.info("task %s created (context %s)", task.id, context_id)
        return task, queue

    def _snapshot(
        self,
        task_id: str,
        params: MessageSendParams | None = None,
        history_length: int | None = None,
    ) -> Task:
        """A deep copy, so a later mutation by the job can't change a response in flight."""
        task = self.store.tasks[task_id].model_copy(deep=True)
        if history_length is None and params and params.configuration:
            history_length = params.configuration.historyLength
        if history_length is not None:
            task.history = task.history[-history_length:] if history_length > 0 else []
        return task

    def _set_state(self, task: Task, state: TaskState, message: Message | None = None) -> None:
        task.status = TaskStatus(state=state, message=message)
        self.bus.publish(
            task.id,
            TaskStatusUpdateEvent(
                taskId=task.id,
                contextId=task.contextId,
                status=task.status.model_copy(deep=True),
                final=state in TERMINAL_STATES,
            ),
        )
        log.info("task %s -> %s", task.id, state.value)

    async def _run(self, task_id: str, user_msg: Message) -> None:
        """The job: call the LLM, stream chunks out as artifact updates."""
        task = self.store.tasks[task_id]
        artifact = Artifact(name="reply", description="Assistant reply")
        chunks: list[str] = []
        try:
            self._set_state(task, TaskState.working)

            history = self.store.contexts[task.contextId]
            llm_messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                *history,
                {"role": "user", "content": user_msg.text},
            ]
            async for delta in self.llm.stream(llm_messages):
                chunks.append(delta)
                self.bus.publish(
                    task_id,
                    TaskArtifactUpdateEvent(
                        taskId=task_id,
                        contextId=task.contextId,
                        artifact=Artifact(artifactId=artifact.artifactId, name="reply", parts=[TextPart(text=delta)]),
                        append=len(chunks) > 1,
                        lastChunk=False,
                    ),
                )

            reply = "".join(chunks)
            if not reply.strip():
                raise RuntimeError("the model returned an empty response")

            # close the streamed artifact and store the complete one on the task
            self.bus.publish(
                task_id,
                TaskArtifactUpdateEvent(
                    taskId=task_id,
                    contextId=task.contextId,
                    artifact=Artifact(artifactId=artifact.artifactId, name="reply", parts=[]),
                    append=True,
                    lastChunk=True,
                ),
            )
            artifact.parts = [TextPart(text=reply)]
            task.artifacts = [artifact]

            agent_msg = text_message(Role.agent, reply, taskId=task_id, contextId=task.contextId)
            task.history.append(agent_msg)
            history += [
                {"role": "user", "content": user_msg.text},
                {"role": "assistant", "content": reply},
            ]
            self._set_state(task, TaskState.completed, agent_msg)

        except asyncio.CancelledError:
            self._set_state(task, TaskState.canceled, text_message(Role.agent, "Task canceled by client."))
        except Exception as exc:  # LLM/network error => task fails, request itself succeeded
            log.exception("task %s failed", task_id)
            self._set_state(
                task,
                TaskState.failed,
                text_message(Role.agent, f"Task failed: {type(exc).__name__}: {exc}", taskId=task_id, contextId=task.contextId),
            )
        finally:
            self.store.jobs.pop(task_id, None)
