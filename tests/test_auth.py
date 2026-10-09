"""OIDC auth tests: real A2AClient + real FastAPI app + an in-process fake identity provider.

No network, no Docker. (tests/test_auth_live.py does the same against mock-oauth2-server.)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlencode
from dataclasses import replace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from a2a_demo.client.auth import A2AAuthError, AuthorizationCodePKCE, ClientCredentials, StaticBearer
from a2a_demo.client.client import A2AClient
from a2a_demo.protocol import AgentCard, Task, TaskArtifactUpdateEvent, TaskState
from a2a_demo.server.app import create_app
from tests.test_a2a import SETTINGS, FakeLLM

ISSUER = "http://idp.test/a2a"
AUTH_SETTINGS = replace(SETTINGS, oidc_issuer=ISSUER, oidc_audience="a2a-agent", oidc_scope="a2a.invoke")
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def mint(key=KEY, kid="k1", alg="RS256", **override) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": "a2a-agent", "sub": "tester", "scope": "a2a.invoke", "iat": now, "exp": now + 300}
    claims.update(override)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def issue_user_tokens(app: FastAPI, expires_in: int = 300) -> dict:
    refresh = secrets.token_urlsafe(8)
    app.state.refresh_tokens.add(refresh)
    app.state.token_calls += 1
    return {"access_token": mint(sub="alice"), "refresh_token": refresh, "token_type": "Bearer", "expires_in": expires_in}


def fake_idp() -> FastAPI:
    """Just enough OIDC provider: discovery, JWKS and a client-credentials token endpoint."""
    app = FastAPI()
    app.state.token_calls = 0
    app.state.codes = {}  # authorization code -> authorize request params
    app.state.refresh_tokens = set()
    app.state.scopes_requested = []
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True) | {"kid": "k1", "use": "sig", "alg": "RS256"}

    @app.get("/a2a/.well-known/openid-configuration")
    async def discovery():
        return {"issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token", "jwks_uri": ISSUER + "/jwks"}

    @app.get("/a2a/authorize")
    async def authorize(request: Request):
        """Auto-approves the "user" alice; remembers the PKCE challenge for the token step."""
        q = dict(request.query_params)
        assert q["code_challenge_method"] == "S256" and q["response_type"] == "code"
        code = secrets.token_urlsafe(8)
        app.state.codes[code] = q
        return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}")

    @app.get("/a2a/jwks")
    async def jwks():
        return {"keys": [jwk]}

    @app.post("/a2a/token")
    async def token(request: Request):
        basic = request.headers.get("authorization", "")
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        grant = form.get("grant_type")
        if grant == "authorization_code":  # public client: PKCE replaces the client secret
            q = app.state.codes.pop(form.get("code"), None)  # codes are single-use
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if not q or q["code_challenge"] != challenge or q["redirect_uri"] != form.get("redirect_uri"):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            app.state.scopes_requested.append(q["scope"])
            return issue_user_tokens(app)
        if grant == "refresh_token":
            if form.get("refresh_token") not in app.state.refresh_tokens:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return issue_user_tokens(app)
        want = "Basic " + base64.b64encode(b"agent-client:s3cret").decode()
        if basic != want or form.get("grant_type") != "client_credentials":
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        app.state.token_calls += 1
        return {"access_token": mint(scope=form.get("scope")), "token_type": "Bearer", "expires_in": 300}

    return app


def run(coro_fn, *, settings=AUTH_SETTINGS, auth=None):
    """Run `coro_fn(client, idp_app)`; `auth` is a factory taking the IdP http client."""

    async def _go():
        idp = fake_idp()
        idp_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=idp))
        app = create_app(settings, FakeLLM(), idp_http=idp_http)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            client = A2AClient("http://test", http_client=http, auth=auth(idp_http) if auth else None)
            await client.get_agent_card()
            client._rpc_url = "http://test/"
            return await coro_fn(client, idp)

    return asyncio.run(_go())


def creds(idp_http):
    return ClientCredentials("agent-client", "s3cret", http_client=idp_http)


# --------------------------------------------------------------------------- #
def test_card_declares_oidc_and_stays_public():
    async def go(c, idp):
        scheme = c.card.securitySchemes["oidc"]
        assert scheme["type"] == "openIdConnect"
        assert scheme["openIdConnectUrl"] == ISSUER + "/.well-known/openid-configuration"
        assert c.card.security == [{"oidc": ["a2a.invoke"]}]

    run(go)  # fetching the card needed no token


def test_no_security_in_card_when_auth_disabled():
    async def go(c, idp):
        assert c.card.security is None and c.card.securitySchemes is None
        assert isinstance(await c.send_message("hi"), Task)  # still open

    run(go, settings=SETTINGS)


def test_client_credentials_flow_end_to_end():
    async def go(c, idp):
        t = await c.send_message("hi")
        assert t.status.state == TaskState.completed
        await c.send_message("again")
        assert idp.state.token_calls == 1  # token is cached between calls

    run(go, auth=creds)


def test_streaming_with_auth():
    async def go(c, idp):
        events = [e async for e in c.stream_message("hi")]
        assert any(isinstance(e, TaskArtifactUpdateEvent) for e in events)

    run(go, auth=creds)


def test_request_without_token_is_401_with_challenge():
    async def go(c, idp):
        r = await c._http.post("http://test/", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "x"}})
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Bearer")
        with pytest.raises(A2AAuthError, match="no credentials configured"):
            await c.send_message("hi")  # the client explains what is wrong

    run(go)


@pytest.mark.parametrize(
    "token, status",
    [
        (mint(exp=int(time.time()) - 10), 401),  # expired
        (mint(aud="someone-else"), 401),  # token minted for another API
        (mint(iss="http://evil/a2a"), 401),  # other issuer
        (mint(key=OTHER_KEY), 401),  # signed with a key the IdP never published
        (mint(kid="unknown"), 401),  # unknown kid
        (mint(scope="read"), 403),  # valid token, missing scope
        (mint(scope=None), 403),
        ("not-a-jwt", 401),
        (jwt.encode({"iss": ISSUER, "aud": "a2a-agent", "exp": int(time.time()) + 99}, "x" * 32, algorithm="HS256", headers={"kid": "k1"}), 401),  # alg confusion
    ],
)
def test_bad_tokens_rejected(token, status):
    async def go(c, idp):
        r = await c._http.post(
            "http://test/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "x"}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == status, r.text
        assert "www-authenticate" in r.headers

    run(go)


def test_scope_may_be_in_scp_claim_as_list():
    async def go(c, idp):
        r = await c._http.post(
            "http://test/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "x"}},
            headers={"Authorization": "Bearer " + mint(scope=None, scp=["a2a.invoke"])},
        )
        assert r.status_code == 200  # authenticated; the JSON-RPC error is a normal 200 body

    run(go)


def test_client_refreshes_token_once_on_401():
    class ExpiredFirst:
        calls: list[bool] = []

        async def token(self, card, *, refresh=False):
            self.calls.append(refresh)
            return mint(exp=int(time.time()) - 10) if not refresh else mint()

    src = ExpiredFirst()

    async def go(c, idp):
        assert (await c.send_message("hi")).status.state == TaskState.completed
        assert src.calls == [False, True]

    run(go, auth=lambda _: src)


def test_static_bearer_wrong_token_surfaces_auth_error():
    async def go(c, idp):
        with pytest.raises(A2AAuthError, match="401"):
            await c.send_message("hi")

    run(go, auth=lambda _: StaticBearer("junk"))


def test_wrong_client_secret_fails_at_token_endpoint():
    async def go(c, idp):
        with pytest.raises(A2AAuthError, match="token endpoint"):
            await c.send_message("hi")

    run(go, auth=lambda h: ClientCredentials("agent-client", "wrong", http_client=h))


# --------------------------------------------------------------------------- #
# authorization code + PKCE (a user logs in; here a fake "browser" does it)
# --------------------------------------------------------------------------- #
def browser_for(idp_app: FastAPI, *, tamper_state: bool = False):
    """open_browser callback: what a real browser does -- visit the authorize URL at the IdP,
    follow its redirect to our loopback redirect_uri."""

    def open_browser(url: str) -> None:
        async def drive() -> None:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=idp_app)) as idp:
                location = (await idp.get(url)).headers["location"]
            if tamper_state:
                location = location.replace("state=", "state=x")
            async with httpx.AsyncClient() as loopback:  # a real socket: our callback listener
                await loopback.get(location)

        asyncio.get_running_loop().create_task(drive())

    return open_browser


def card_requiring_oidc() -> AgentCard:
    return AgentCard.model_validate({
        "name": "a", "description": "d", "url": "http://x/", "version": "1", "capabilities": {},
        "securitySchemes": {"oidc": {"type": "openIdConnect",
                                     "openIdConnectUrl": ISSUER + "/.well-known/openid-configuration"}},
        "security": [{"oidc": ["a2a.invoke"]}]})


def pkce_source(idp, **kw) -> AuthorizationCodePKCE:
    kw.setdefault("open_browser", browser_for(idp))
    return AuthorizationCodePKCE("a2a-cli", http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=idp)),
                                 announce=lambda _: None, **kw)


def test_login_with_pkce_end_to_end():
    async def _run():
        idp = fake_idp()
        idp_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=idp))
        app = create_app(AUTH_SETTINGS, FakeLLM(), idp_http=idp_http)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            c = A2AClient("http://test", http_client=http, auth=pkce_source(idp))
            await c.get_agent_card()
            c._rpc_url = "http://test/"
            assert (await c.send_message("hi")).status.state == TaskState.completed
            assert idp.state.scopes_requested == ["openid a2a.invoke"]  # `openid` + the card's scopes
            await c.send_message("again")
            assert idp.state.token_calls == 1  # one login, token reused

    asyncio.run(_run())


def test_pkce_wrong_verifier_is_rejected_by_idp():
    """What PKCE protects against: someone who stole the code but does not know the verifier."""

    async def _run():
        idp = fake_idp()
        idp.state.codes["stolen"] = {"code_challenge": "hash-of-someone-elses-verifier", "redirect_uri": "http://x"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=idp)) as http:
            r = await http.post(ISSUER + "/token", data={"grant_type": "authorization_code", "code": "stolen",
                                "redirect_uri": "http://x", "code_verifier": "attacker-guess"})
            assert r.status_code == 400

    asyncio.run(_run())


def test_login_rejects_callback_with_wrong_state():
    async def _run():
        idp = fake_idp()
        src = pkce_source(idp, open_browser=browser_for(idp, tamper_state=True))
        with pytest.raises(A2AAuthError, match="state"):
            await src.token(card_requiring_oidc())

    asyncio.run(_run())


def test_login_timeout_when_user_never_finishes():
    async def _run():
        src = pkce_source(fake_idp(), open_browser=lambda url: None, login_timeout=0.2)
        with pytest.raises(A2AAuthError, match="timed out"):
            await src.token(card_requiring_oidc())

    asyncio.run(_run())


def test_expired_token_is_renewed_with_refresh_token_without_new_login():
    async def _run():
        idp = fake_idp()
        logins = []
        browser = browser_for(idp)
        src = pkce_source(idp, open_browser=lambda url: (logins.append(url), browser(url)))
        card = card_requiring_oidc()
        first = await src.token(card)
        assert (await src.token(card)) == first and len(logins) == 1  # cached
        second = await src.token(card, refresh=True)  # as the client does after a 401
        assert len(logins) == 1  # refresh_token used: the browser did not open again
        assert jwt.decode(second, options={"verify_signature": False})["sub"] == "alice"
        src._refresh_token = "revoked"  # refresh token no longer valid -> full login again
        await src.token(card, refresh=True)
        assert len(logins) == 2

    asyncio.run(_run())
