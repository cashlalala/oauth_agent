"""A2A protocol data model (a hand-written subset of the A2A spec, v0.3 wire format).

Everything that travels between an A2A client and an A2A server is defined here, so
this file is the best place to start reading. Field names are deliberately camelCase
(messageId, contextId, ...) because that is what appears on the wire.

Concept map
-----------
AgentCard            "business card" served at /.well-known/agent-card.json (discovery)
Message              one turn of communication (role = user | agent) made of Parts
Part                 a piece of content inside a Message/Artifact (we implement TextPart)
Task                 the unit of work with a lifecycle (TaskState); has history + artifacts
Artifact             the *output* of a task (e.g. the chat reply), delivered in Parts
TaskStatusUpdateEvent / TaskArtifactUpdateEvent
                     the events pushed over SSE by `message/stream`
A2AError             JSON-RPC 2.0 error carrying an A2A / JSON-RPC error code
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = "0.3.0"
AGENT_CARD_PATH = "/.well-known/agent-card.json"


def new_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class A2AModel(BaseModel):
    # Be liberal in what we accept: unknown fields are kept, not rejected.
    model_config = ConfigDict(extra="allow")

    def to_wire(self) -> dict[str, Any]:
        """Serialize to the JSON dict that goes on the wire (omit None fields)."""
        return self.model_dump(mode="json", exclude_none=True)


# --------------------------------------------------------------------------- #
# Messages and parts
# --------------------------------------------------------------------------- #
class Role(str, Enum):
    user = "user"
    agent = "agent"


class TextPart(A2AModel):
    """The only Part type this demo implements. (The spec also has FilePart/DataPart.)"""

    kind: Literal["text"] = "text"
    text: str
    metadata: dict[str, Any] | None = None


class Message(A2AModel):
    kind: Literal["message"] = "message"
    messageId: str = Field(default_factory=new_id)
    role: Role
    parts: list[TextPart]
    contextId: str | None = None  # groups several tasks into one conversation
    taskId: str | None = None  # set when the message belongs to a task
    metadata: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        return "".join(p.text for p in self.parts)


def text_message(role: Role, text: str, **kw: Any) -> Message:
    return Message(role=role, parts=[TextPart(text=text)], **kw)


# --------------------------------------------------------------------------- #
# Task, status, artifact
# --------------------------------------------------------------------------- #
class TaskState(str, Enum):
    submitted = "submitted"  # received, not started yet
    working = "working"  # being processed
    input_required = "input-required"  # agent needs more input from the client
    completed = "completed"  # terminal: success
    canceled = "canceled"  # terminal: canceled by tasks/cancel
    failed = "failed"  # terminal: error
    rejected = "rejected"  # terminal: agent refused the task
    auth_required = "auth-required"
    unknown = "unknown"


TERMINAL_STATES = {
    TaskState.completed,
    TaskState.canceled,
    TaskState.failed,
    TaskState.rejected,
}


class TaskStatus(A2AModel):
    state: TaskState
    message: Message | None = None  # e.g. the final agent reply, or an error text
    timestamp: str = Field(default_factory=now_iso)


class Artifact(A2AModel):
    artifactId: str = Field(default_factory=new_id)
    name: str | None = None
    description: str | None = None
    parts: list[TextPart] = Field(default_factory=list)


class Task(A2AModel):
    kind: Literal["task"] = "task"
    id: str = Field(default_factory=new_id)
    contextId: str = Field(default_factory=new_id)
    status: TaskStatus
    history: list[Message] = Field(default_factory=list)
    artifacts: list[Artifact] | None = None
    metadata: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Streaming events (message/stream)
# --------------------------------------------------------------------------- #
class TaskStatusUpdateEvent(A2AModel):
    kind: Literal["status-update"] = "status-update"
    taskId: str
    contextId: str
    status: TaskStatus
    final: bool = False  # True => last event of the stream


class TaskArtifactUpdateEvent(A2AModel):
    kind: Literal["artifact-update"] = "artifact-update"
    taskId: str
    contextId: str
    artifact: Artifact
    append: bool = False  # True => append parts to the artifact with the same id
    lastChunk: bool = False  # True => this artifact is complete


StreamEvent = Task | Message | TaskStatusUpdateEvent | TaskArtifactUpdateEvent


def parse_result(obj: dict[str, Any]) -> StreamEvent:
    """Turn a JSON-RPC `result` into the right model by looking at its `kind`."""
    kind = obj.get("kind")
    models: dict[str, type[A2AModel]] = {
        "task": Task,
        "message": Message,
        "status-update": TaskStatusUpdateEvent,
        "artifact-update": TaskArtifactUpdateEvent,
    }
    if kind not in models:
        raise ValueError(f"unknown result kind: {kind!r}")
    return models[kind].model_validate(obj)  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Method parameters
# --------------------------------------------------------------------------- #
class MessageSendConfiguration(A2AModel):
    acceptedOutputModes: list[str] | None = None
    historyLength: int | None = None  # how many history messages to return
    blocking: bool | None = None  # False => return immediately, client polls


class MessageSendParams(A2AModel):
    message: Message
    configuration: MessageSendConfiguration | None = None
    metadata: dict[str, Any] | None = None


class TaskQueryParams(A2AModel):
    id: str
    historyLength: int | None = None


class TaskIdParams(A2AModel):
    id: str


# --------------------------------------------------------------------------- #
# Agent Card (discovery)
# --------------------------------------------------------------------------- #
class AgentCapabilities(A2AModel):
    streaming: bool = False
    pushNotifications: bool = False
    stateTransitionHistory: bool = False


class AgentSkill(A2AModel):
    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    inputModes: list[str] | None = None
    outputModes: list[str] | None = None


class AgentCard(A2AModel):
    protocolVersion: str = PROTOCOL_VERSION
    name: str
    description: str
    url: str  # endpoint that accepts JSON-RPC POSTs
    preferredTransport: str = "JSONRPC"
    version: str
    capabilities: AgentCapabilities
    defaultInputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    defaultOutputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    skills: list[AgentSkill] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# JSON-RPC 2.0 errors
# --------------------------------------------------------------------------- #
class ErrorCode(int, Enum):
    # standard JSON-RPC
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    # A2A specific
    TASK_NOT_FOUND = -32001
    TASK_NOT_CANCELABLE = -32002
    PUSH_NOT_SUPPORTED = -32003
    UNSUPPORTED_OPERATION = -32004


class A2AError(Exception):
    """Raised by handlers; the server turns it into a JSON-RPC error object,
    the client raises it again when it receives one."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"[{code}] {message}")
        self.code = int(code)
        self.message = message
        self.data = data

    def to_wire(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


# --------------------------------------------------------------------------- #
# JSON-RPC envelopes
# --------------------------------------------------------------------------- #
def rpc_request(method: str, params: dict[str, Any], req_id: str | int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}


def rpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    if isinstance(result, A2AModel):
        result = result.to_wire()
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def rpc_error(req_id: Any, err: A2AError) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": err.to_wire()}
