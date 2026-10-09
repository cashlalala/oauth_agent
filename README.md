# A2A learning project: chat agent server + client (Python)

A hand-written implementation of the core of the **A2A (Agent2Agent) protocol** — no A2A SDK,
so every byte on the wire is visible in the code. The agent's "brain" is an OpenAI-compatible
client (`openai` lib) pointed at **OpenRouter** (`openrouter/free` model router).

## Quick start (Windows PowerShell)

```powershell
python -m venv .venv                  # already created
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# .env holds OPENROUTER_API_KEY (see .env.example); it is git-ignored

python -m a2a_demo.server             # terminal 1: agent on http://127.0.0.1:9999
python -m a2a_demo.client             # terminal 2: interactive chat (try /trace)
python demo.py                        # terminal 2: guided tour of every protocol feature
pytest                                # offline tests with a fake LLM (no network)
```

## Layout

```
a2a_demo/
  protocol.py        all A2A types (Message, Task, Artifact, events, AgentCard) + JSON-RPC helpers/errors
  config.py          settings from .env
  server/
    app.py           FastAPI: Agent Card endpoint, JSON-RPC dispatcher, SSE streaming
    handler.py       the protocol logic: task lifecycle, message/send|stream, tasks/get|cancel
    store.py         in-memory tasks, per-context chat history, EventBus
    llm.py           OpenRouter via openai.AsyncOpenAI (streaming)
  client/
    client.py        A2AClient: discovery, JSON-RPC calls, SSE parsing
    cli.py           interactive chat (/stream /async /task /cancel /trace /card /new)
demo.py              scripted tour with wire-level traces
tests/test_a2a.py    client <-> server tests, in-process, fake LLM
```

## A2A concepts and where they live

| Concept | What it is | Code |
|---|---|---|
| **Agent Card** | JSON "business card" at `/.well-known/agent-card.json`: name, endpoint `url`, capabilities (`streaming`), skills. Clients discover agents with it. | `server/app.py::build_agent_card`, `client.get_agent_card` |
| **JSON-RPC 2.0** | Transport format: `POST /` with `{jsonrpc, id, method, params}`. Errors are HTTP 200 with an `error` object. | `app.py::jsonrpc`, `client.rpc` |
| **Message** | One turn, `role` = `user`/`agent`, content in `parts` (we implement `TextPart`). | `protocol.Message` |
| **Task** | Unit of work with an id and a **state machine**: `submitted → working → completed / failed / canceled`. | `protocol.Task`, `handler._run` |
| **Artifact** | The *output* of a task (here: the reply). Kept separate from the messages (history). | `protocol.Artifact` |
| **contextId** | Groups several tasks into one conversation. Same `contextId` = the agent remembers; each message still creates a **new task id**. | `store.contexts`, demo step 3 |
| **message/send** | Request/response. `blocking=true` waits for the final Task; `blocking=false` returns a `submitted` Task immediately and the client polls. | `handler.message_send` |
| **message/stream** | Same call but the response is an SSE stream: `Task` → `status-update(working)` → `artifact-update` chunks (`append`) → `status-update(completed, final=true)`. | `handler.message_stream`, `client.rpc_stream` |
| **tasks/get** | Poll a task's current state. | `handler.tasks_get` |
| **tasks/cancel** | Cancel a running task (cancels the asyncio job; state → `canceled`). | `handler.tasks_cancel` |
| **Errors** | `-32700/-32600/-32601/-32602/-32603` (JSON-RPC) and `-32001 TaskNotFound`, `-32002 TaskNotCancelable` (A2A). | `protocol.ErrorCode` |

## Request flow (streaming)

```
client                                   server                          OpenRouter
  | GET /.well-known/agent-card.json ---->|
  |<-------------------------- AgentCard -|
  | POST / message/stream {message} ----->| validate -> create Task(submitted)
  |<-- SSE: Task(submitted) --------------| start job (asyncio) ---------------> chat.completions(stream)
  |<-- SSE: status-update working ---------|<------------------------------ delta "Hel"
  |<-- SSE: artifact-update "Hel" ---------|<------------------------------ delta "lo"
  |<-- SSE: artifact-update "lo" (append) -|
  |<-- SSE: status-update completed final -| save artifact + history + context memory
```

The job and the HTTP request are decoupled through an **EventBus**: the job publishes events;
a stream request (or blocking send) subscribes; a non-blocking send doesn't, so the work just
carries on and `tasks/get` reads the stored state. This is also why `tasks/cancel` from a
*second* connection can stop a task that a first connection is streaming (demo step 6).

## Deliberate simplifications (good next exercises)

- Only `TextPart` (spec also has `FilePart`, `DataPart`).
- Task state is in memory — restart loses it. Swap `TaskStore` for SQLite/Redis.
- No `input-required` flow, `tasks/resubscribe`, push notifications, or authenticated extended card.
  The agent card declares no `securitySchemes`; add a bearer-token check in `app.py` to try it.
- Only the JSON-RPC transport (spec also defines gRPC and HTTP+JSON).
- Try next: a second agent that is both an A2A *server* and a *client* of this one (agent chaining).

## Notes

- Key handling: the OpenRouter key is only in `.env` (git-ignored), never in source. Since it was
  shared in plain text, consider rotating it at https://openrouter.ai/keys.
- `openrouter/free` routes to whichever free model is available, so replies vary and free models
  can be rate-limited; in that case the task ends `failed` with the error text in `status.message`.
