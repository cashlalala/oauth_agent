"""Web UI tests: a simulated browser -> real web app -> real A2AClient -> real agent app (fake LLM),
with the in-process fake IdP from test_auth. No network, no Docker."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

import httpx

from a2a_demo.config import WebSettings
from a2a_demo.server.app import create_app as create_agent
from a2a_demo.web.app import create_app as create_web
from tests.test_a2a import SETTINGS, FakeLLM
from tests.test_auth import AUTH_SETTINGS, fake_idp, mint

WEB = WebSettings(agent_url="http://agent.test", host="127.0.0.1", port=8000,
                  public_url="http://web.test", client_id="a2a-web")


def run(coro_fn, *, agent_settings=AUTH_SETTINGS):
    """`coro_fn(browser, idp, idp_http)`; `browser` is an httpx client with a cookie jar."""

    async def _go():
        idp = fake_idp()
        idp_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=idp))
        agent = create_agent(agent_settings, FakeLLM(), idp_http=idp_http)
        agent_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=agent), base_url="http://agent.test")
        web = create_web(WEB, agent_http=agent_http, idp_http=idp_http)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://web.test") as browser:
            return await coro_fn(browser, idp, idp_http)

    return asyncio.run(_go())


async def log_in(browser, idp_http) -> httpx.Response:
    """What the browser does: /auth/login -> IdP -> back to /auth/callback."""
    r = await browser.get("/auth/login")
    assert r.status_code == 307
    r = await idp_http.get(r.headers["location"])  # the IdP auto-approves and redirects
    back = urlsplit(r.headers["location"])
    assert back.path == "/auth/callback"
    return await browser.get(f"{back.path}?{back.query}")


def events(resp: httpx.Response) -> list[dict]:
    return [json.loads(l[5:]) for l in resp.text.splitlines() if l.startswith("data:")]


def test_login_then_chat_as_the_user():
    async def go(browser, idp, idp_http):
        assert (await browser.get("/api/me")).json()["user"] is None
        assert (await browser.post("/api/chat", json={"text": "hi"})).status_code == 401  # login first

        r = await log_in(browser, idp_http)
        assert r.status_code == 307 and r.headers["location"] == "/"
        assert idp.state.scopes_requested[0].split()[0] == "openid"

        me = (await browser.get("/api/me")).json()
        assert me["loginRequired"] and me["user"]["sub"] == "alice"

        r = await browser.post("/api/chat", json={"text": "hello"})
        assert r.headers["content-type"].startswith("text/event-stream")
        evs = events(r)
        assert evs[0]["kind"] == "task" and evs[-1]["kind"] == "status-update" and evs[-1]["final"]
        assert evs[-1]["status"]["state"] == "completed"
        assert any(e["kind"] == "artifact-update" for e in evs)

        # the same conversation continues; someone else's context is refused
        ctx = evs[0]["contextId"]
        assert (await browser.post("/api/chat", json={"text": "again", "contextId": ctx})).status_code == 200
        assert (await browser.post("/api/chat", json={"text": "x", "contextId": "nope"})).status_code == 403

    run(go)


def test_tokens_never_reach_the_browser():
    async def go(browser, idp, idp_http):
        await log_in(browser, idp_http)
        blob = (await browser.get("/api/me")).text + "".join(c.value for c in browser.cookies.jar)
        assert "eyJ" not in blob  # no JWT anywhere; the cookie is an opaque session id
        cookie = next(iter(browser.cookies.jar))
        assert cookie.has_nonstandard_attr("HttpOnly")

    run(go)


def test_callback_with_wrong_state_is_rejected():
    async def go(browser, idp, idp_http):
        await browser.get("/auth/login")
        r = await browser.get("/auth/callback?code=x&state=forged")
        assert "login_error" in r.headers["location"]
        assert (await browser.get("/api/me")).json()["user"] is None

    run(go)


def test_callback_without_login_started():
    async def go(browser, idp, idp_http):
        r = await browser.get("/auth/callback?code=x&state=y")
        assert "login_error" in r.headers["location"]

    run(go)


def test_logout_drops_the_session():
    async def go(browser, idp, idp_http):
        await log_in(browser, idp_http)
        await browser.post("/auth/logout")
        assert (await browser.get("/api/me")).json()["user"] is None
        assert (await browser.post("/api/chat", json={"text": "hi"})).status_code == 401

    run(go)


def test_expired_token_is_refreshed_silently():
    async def go(browser, idp, idp_http):
        await log_in(browser, idp_http)
        session = next(iter(browser._transport.app.state.sessions.values()))
        before = idp.state.token_calls
        session.expires_at = 0  # pretend the access token just expired
        assert (await browser.post("/api/chat", json={"text": "hi"})).status_code == 200
        assert idp.state.token_calls == before + 1  # one refresh_token grant

    run(go)


def test_dead_refresh_token_sends_user_back_to_login():
    async def go(browser, idp, idp_http):
        await log_in(browser, idp_http)
        session = next(iter(browser._transport.app.state.sessions.values()))
        session.expires_at = 0
        idp.state.refresh_tokens.clear()  # IdP forgot the refresh token
        assert (await browser.post("/api/chat", json={"text": "hi"})).status_code == 401
        assert (await browser.get("/api/me")).json()["user"] is None  # page falls back to the login gate

    run(go)


def test_open_agent_needs_no_login():
    async def go(browser, idp, idp_http):
        me = (await browser.get("/api/me")).json()
        assert me["loginRequired"] is False
        r = await browser.post("/api/chat", json={"text": "hi"})
        assert r.status_code == 200 and events(r)[-1]["status"]["state"] == "completed"
        ctx = events(r)[0]["contextId"]
        # the conversation is still bound to this browser
        assert (await browser.post("/api/chat", json={"text": "more", "contextId": ctx})).status_code == 200

    run(go, agent_settings=SETTINGS)


def test_agent_down_is_a_502_not_a_crash():
    async def go():
        dead = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("down"))))
        web = create_web(WEB, agent_http=dead)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://web.test") as b:
            assert (await b.get("/api/me")).status_code == 502

    asyncio.run(go())


def test_static_page_served():
    async def go(browser, idp, idp_http):
        assert "<title>A2A Chat</title>" in (await browser.get("/")).text
        assert (await browser.get("/static/app.js")).status_code == 200

    run(go)
