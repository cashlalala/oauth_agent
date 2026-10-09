# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A learning project: a hand-written subset of the A2A (Agent2Agent) protocol (JSON-RPC 2.0 over HTTP + SSE) with **no A2A SDK on purpose**, so the wire format stays visible in the code. The agent's LLM is the `openai` library pointed at OpenRouter (`openrouter/free`). Keep it SDK-free and readable; this is a teaching codebase. READMEs exist in English and Traditional Chinese (`README.zh-TW.md`) — keep them in sync when behavior changes.

## Commands

Windows; always use the project virtualenv (`.venv`).

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python -m a2a_demo.server          # agent server, default http://127.0.0.1:9999 (needs OPENROUTER_API_KEY in .env)
python -m a2a_demo.client          # interactive chat client (--no-stream, --trace, --url)
python demo.py                     # scripted protocol tour; requires the server to be running

pytest                             # all tests, offline (fake LLM)
pytest tests/test_a2a.py::test_stream_event_order   # single test
```

No linter/formatter is configured. Settings come from `.env` (see `.env.example`); `.env` is git-ignored.

## Architecture

- `a2a_demo/protocol.py` is the single source of truth for wire types (pydantic, **camelCase field names on purpose** to match the spec), JSON-RPC envelope helpers, and `A2AError`/`ErrorCode`. Both server and client import it; `A2AError` is raised by handlers, serialized by the server, and re-raised by the client.
- Server is three layers: `server/app.py` (FastAPI: agent card + one `POST /` JSON-RPC dispatcher, SSE framing) → `server/handler.py` (`A2AHandler`: protocol logic and task lifecycle) → `server/llm.py` (`ChatBackend` protocol; `OpenRouterChat` is the real impl). `create_app(settings, llm)` accepts an injected backend; tests rely on this.
- **Job / EventBus decoupling** (the key idea, spans `handler.py` + `store.py`): every incoming message creates a `Task` and an `asyncio` job (`A2AHandler._run`) that publishes status/artifact events to `EventBus`. Blocking `message/send` and `message/stream` *subscribe* to consume events; non-blocking send doesn't subscribe and the client polls `tasks/get`. Subscription must happen **before** the job is created so no events are missed. `tasks/cancel` cancels the job from a separate request; `_run` catches `CancelledError` and records `canceled` (it deliberately does not re-raise).
- State is in-memory (`TaskStore`): `tasks`, `jobs`, and `contexts` (contextId → OpenAI-style chat history). Conversation memory is keyed by `contextId`, not task id; each message makes a new task. Terminal tasks (and any existing `taskId`, since the agent never enters `input-required`) reject further messages with `-32602`.
- LLM/network failures do not fail the JSON-RPC request: the task ends in state `failed` with the error in `status.message`. JSON-RPC errors are returned as HTTP 200.
- `message/stream` validates and starts the job *before* opening the SSE response so protocol errors still come back as plain JSON-RPC errors (the client handles a non-`text/event-stream` response for this reason). First SSE event is the `Task`, then status/artifact updates, ending with `final=true`.
- Client: `client/client.py` (`A2AClient`) takes an optional injected `httpx.AsyncClient` and a `trace(direction, payload)` callback; the CLI and `demo.py` use the trace hook to print raw JSON-RPC. The agent card's `url` overrides the RPC endpoint after discovery.

## Testing notes

`tests/test_a2a.py` runs the real `A2AClient` against the real FastAPI app in-process via `httpx.ASGITransport` with a `FakeLLM`; no network or API key needed. ASGITransport buffers responses, so streaming tests verify event order, not true incremental delivery. New protocol behavior should be covered here with the fake backend, and live-checked with `demo.py`.
