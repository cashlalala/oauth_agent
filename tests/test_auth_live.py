"""Same flow against the real mock-oauth2-server. Skipped unless it is running:  docker compose up -d"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from a2a_demo.client.auth import ClientCredentials
from a2a_demo.client.client import A2AClient
from a2a_demo.protocol import TaskState
from a2a_demo.server.app import create_app
from tests.test_a2a import SETTINGS, FakeLLM

ISSUER = "http://localhost:8080/a2a"
LIVE = replace(SETTINGS, oidc_issuer=ISSUER)


def idp_up() -> bool:
    try:
        return httpx.get(ISSUER + "/.well-known/openid-configuration", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not idp_up(), reason="mock-oauth2-server not running (docker compose up -d)")


def test_against_mock_oauth2_server():
    async def go():
        async with httpx.AsyncClient() as idp_http:  # real network to the container
            app = create_app(LIVE, FakeLLM(), idp_http=idp_http)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                c = A2AClient("http://test", http_client=http, auth=ClientCredentials("agent-client", "x", http_client=idp_http))
                await c.get_agent_card()
                c._rpc_url = "http://test/"
                assert (await c.send_message("hi")).status.state == TaskState.completed

                bare = await http.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "x"}})
                assert bare.status_code == 401

                # a token the mock issues for a different audience / without our scope is refused
                r = await idp_http.post(ISSUER + "/token", data={"grant_type": "authorization_code", "code": "c",
                                        "client_id": "x", "client_secret": "x", "redirect_uri": "http://x"})
                tok = r.json().get("access_token")
                if tok:
                    wrong = await http.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "x"}},
                                            headers={"Authorization": f"Bearer {tok}"})
                    assert wrong.status_code in (401, 403)

    asyncio.run(go())


def headless_browser(url: str) -> None:
    """Plays the user: the mock shows an HTML login form at the authorize URL, we submit it as 'alice'."""

    async def drive() -> None:
        async with httpx.AsyncClient() as browser:
            r = await browser.post(url, data={"username": "alice"})  # form posts back to the same URL
            assert r.status_code == 302, r.text[:200]
            await browser.get(r.headers["location"])  # redirect -> our loopback callback

    asyncio.get_running_loop().create_task(drive())


def test_user_login_pkce_against_mock_oauth2_server():
    from a2a_demo.client.auth import AuthorizationCodePKCE
    import jwt

    async def go():
        async with httpx.AsyncClient() as idp_http:
            app = create_app(LIVE, FakeLLM(), idp_http=idp_http)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
                src = AuthorizationCodePKCE("a2a-cli", http_client=idp_http, open_browser=headless_browser, announce=lambda _: None)
                c = A2AClient("http://test", http_client=http, auth=src)
                await c.get_agent_card()
                c._rpc_url = "http://test/"
                assert (await c.send_message("hi")).status.state == TaskState.completed
                claims = jwt.decode(await src.token(c.card), options={"verify_signature": False})
                assert claims["sub"] == "alice" and claims["aud"] == "a2a-agent"  # the token is the USER's
                assert src._refresh_token  # the mock hands out a refresh token

    asyncio.run(go())
