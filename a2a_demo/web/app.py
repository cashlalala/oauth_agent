"""Web UI backend: static page + a tiny JSON API, in front of the A2A agent.

    browser  --(session cookie)-->  this app  --(Authorization: Bearer <user's token>)-->  A2A agent

The browser never speaks A2A and never sees a token. This app is an ordinary `A2AClient` whose
`TokenSource` is the logged-in user's `WebSession`. Routes:

    GET  /                  the page (static/)
    GET  /auth/login        redirect the browser to the IdP (authorization code + PKCE)
    GET  /auth/callback     the IdP sends the browser back here with ?code=&state=
    POST /auth/logout       forget the session
    GET  /api/me            agent card summary + who is logged in
    POST /api/chat          {text, contextId?} -> SSE stream of the raw A2A events (message/stream)
    POST /api/cancel        {taskId} -> tasks/cancel
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..client.auth import A2AAuthError
from ..client.client import A2AClient
from ..config import WebSettings, load_web_settings
from ..protocol import A2AError, AgentCard, Task
from .auth import WebLogin, WebSession

log = logging.getLogger("a2a.web")

STATIC = Path(__file__).parent / "static"
COOKIE = "a2a_web_sid"


def sse(payload: dict[str, Any], event: str | None = None) -> str:
    return (f"event: {event}\n" if event else "") + f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def create_app(
    settings: WebSettings | None = None,
    agent_http: httpx.AsyncClient | None = None,
    idp_http: httpx.AsyncClient | None = None,
) -> FastAPI:
    """`agent_http` / `idp_http` are the HTTP clients used to reach the agent / the IdP (injectable for tests)."""
    settings = settings or load_web_settings()
    agent_http = agent_http or httpx.AsyncClient(timeout=120)
    login = WebLogin(settings, idp_http or httpx.AsyncClient(timeout=10))
    sessions: dict[str, WebSession] = {}  # in memory, like the agent's TaskStore
    app = FastAPI(title="A2A web UI")
    app.state.sessions = sessions  # for tests
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ---- helpers ----------------------------------------------------------
    def session_of(request: Request) -> WebSession | None:
        return sessions.get(request.cookies.get(COOKIE, ""))

    def set_cookie(resp: Response, session: WebSession) -> None:
        resp.set_cookie(COOKIE, session.sid, httponly=True, samesite="lax", path="/",
                        secure=settings.public_url.startswith("https://"))

    def error(status: int, code: str, message: str = "") -> JSONResponse:
        return JSONResponse({"error": code, "message": message}, status_code=status)

    async def open_client(session: WebSession | None) -> tuple[A2AClient, AgentCard]:
        """Fetch the card (public) and, if it declares security, act as the logged-in user."""
        client = A2AClient(settings.agent_url, http_client=agent_http)
        card = await client.get_agent_card()
        if card.security:
            if not session or not session.access_token:
                raise A2AAuthError("login required")
            client.auth = session
        return client, card

    async def fetch_card() -> AgentCard:
        return await A2AClient(settings.agent_url, http_client=agent_http).get_agent_card()

    def failure(e: Exception, session: WebSession | None) -> JSONResponse:
        if isinstance(e, A2AAuthError):
            if session:
                session.access_token = None  # /api/me then reports "logged out" and the page shows Login
            return error(401, "login_required", str(e))
        if isinstance(e, A2AError):
            return error(400, "a2a_error", f"{e.code}: {e.message}")
        return error(502, "agent_unreachable", str(e) or type(e).__name__)

    def login_error(message: str) -> RedirectResponse:
        return RedirectResponse("/?" + urlencode({"login_error": message}))

    # ---- pages ------------------------------------------------------------
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    # ---- login ------------------------------------------------------------
    @app.get("/auth/login")
    async def auth_login(request: Request):
        session = session_of(request) or WebSession()
        try:
            card = await fetch_card()
            if not card.security:
                return RedirectResponse("/")  # open agent: nothing to log in to
            url = await login.start(session, card)
        except (A2AAuthError, httpx.HTTPError, KeyError) as e:
            return login_error(f"cannot start login: {e}")
        sessions[session.sid] = session  # remembers state + PKCE verifier until the callback
        resp = RedirectResponse(url)
        set_cookie(resp, session)
        return resp

    @app.get("/auth/callback")
    async def auth_callback(request: Request):
        session = session_of(request)
        if not session:
            return login_error("no login in progress (cookies blocked, or the server restarted)")
        try:
            await login.finish(session, await fetch_card(), dict(request.query_params))
        except (A2AAuthError, httpx.HTTPError, KeyError) as e:
            return login_error(str(e))
        # new session id after login, so an id planted before login is worthless (session fixation)
        del sessions[session.sid]
        session.sid = WebSession().sid
        sessions[session.sid] = session
        log.info("login ok: sub=%s", (session.user or {}).get("sub"))
        resp = RedirectResponse("/")
        set_cookie(resp, session)
        return resp

    @app.post("/auth/logout")
    async def auth_logout(request: Request) -> JSONResponse:
        if session := session_of(request):
            del sessions[session.sid]
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    # ---- API --------------------------------------------------------------
    @app.get("/api/me")
    async def me(request: Request):
        session = session_of(request)
        try:
            card = await fetch_card()
        except httpx.HTTPError as e:
            return failure(e, session)
        return {
            "agent": {"name": card.name, "description": card.description, "version": card.version,
                      "skills": [s.name for s in card.skills]},
            "loginRequired": bool(card.security),
            "user": session.user if session and session.access_token else None,
        }

    @app.post("/api/chat")
    async def chat(request: Request):
        session = session_of(request)
        body = await request.json()
        text, context_id = str(body.get("text", "")).strip(), body.get("contextId")
        if not text:
            return error(400, "empty", "text is required")
        if context_id and not (session and context_id in session.contexts):
            return error(403, "unknown_context", "that conversation does not belong to this session")
        try:
            client, _ = await open_client(session)
            events = client.stream_message(text, context_id=context_id)
            # take the first event *before* answering, so protocol/auth errors become HTTP errors
            first = await events.__anext__()
        except (A2AAuthError, A2AError, httpx.HTTPError) as e:
            return failure(e, session)

        # An open agent needs no login, but we still remember which conversations are this browser's.
        owner = session or WebSession()
        sessions.setdefault(owner.sid, owner)

        def remember(ev: Any) -> None:
            if getattr(ev, "contextId", None):
                owner.contexts.add(ev.contextId)
            if isinstance(ev, Task):
                owner.tasks.add(ev.id)

        async def stream() -> AsyncIterator[str]:
            try:
                remember(first)
                yield sse(first.to_wire())
                async for ev in events:
                    remember(ev)
                    yield sse(ev.to_wire())
            except (A2AAuthError, A2AError, httpx.HTTPError) as e:  # mid-stream: the 200 is already sent
                yield sse({"error": str(e) or type(e).__name__}, event="error")
            finally:
                await events.aclose()

        resp = StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        if owner is not session:
            set_cookie(resp, owner)
        return resp

    @app.post("/api/cancel")
    async def cancel(request: Request):
        session = session_of(request)
        task_id = (await request.json()).get("taskId")
        if not (session and task_id in session.tasks):
            return error(403, "unknown_task", "that task does not belong to this session")
        try:
            client, _ = await open_client(session)
            task = await client.cancel_task(task_id)
        except (A2AAuthError, A2AError, httpx.HTTPError) as e:
            return failure(e, session)
        return task.to_wire()

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s")
    s = load_web_settings()
    print(f"web UI: {s.public_url}   (agent: {s.agent_url}, OIDC client_id: {s.client_id})")
    print(f"redirect URI used for login: {s.redirect_uri}")
    uvicorn.run(create_app(s), host=s.host, port=s.port, log_level="warning")


if __name__ == "__main__":
    main()
