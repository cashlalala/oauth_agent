"""Client-side auth: learn from the Agent Card that OIDC is needed, then get a token.

Common part:
  1. Agent Card  -> securitySchemes.<name>.openIdConnectUrl, security[].<name> = scopes
  2. GET openIdConnectUrl (OIDC discovery) -> authorization_endpoint, token_endpoint
  3. get an access_token (JWT) -- how depends on WHO is calling:
       ClientCredentials      the client itself (agent-to-agent, no human): POST token_endpoint
       AuthorizationCodePKCE  a human user, via browser login (CLIs, desktop apps)
       StaticBearer           a token you already have
  4. every JSON-RPC request carries  Authorization: Bearer <access_token>
The server cannot tell the difference: it only validates the JWT. The token's `sub` is the
agent (client credentials) or the user (authorization code).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
import webbrowser
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from ..protocol import AgentCard

EXPIRY_SKEW = 30  # refresh a little before the token really expires


class A2AAuthError(Exception):
    """The agent wants credentials we don't have (or rejected the ones we sent)."""


class TokenSource(Protocol):
    async def token(self, card: AgentCard | None, *, refresh: bool = False) -> str: ...


async def _get_json(http: httpx.AsyncClient, url: str) -> dict[str, Any]:
    r = await http.get(url)
    r.raise_for_status()
    return r.json()


class StaticBearer:
    """A token you already have (e.g. pasted from a login flow)."""

    def __init__(self, token: str):
        self._token = token

    async def token(self, card: AgentCard | None, *, refresh: bool = False) -> str:
        return self._token


class ClientCredentials:
    def __init__(self, client_id: str, client_secret: str, *, scope: str | None = None,
                 http_client: httpx.AsyncClient | None = None):
        self.client_id, self.client_secret, self.scope = client_id, client_secret, scope
        self._http = http_client or httpx.AsyncClient(timeout=10)
        self._token: str | None = None
        self._expires_at = 0.0

    async def token(self, card: AgentCard | None, *, refresh: bool = False) -> str:
        if self._token and not refresh and time.monotonic() < self._expires_at:
            return self._token
        if card is None:
            raise A2AAuthError("fetch the agent card first: it says where to get a token")
        discovery_url, scopes = oidc_requirement(card)
        meta = await _get_json(self._http, discovery_url)
        data = {"grant_type": "client_credentials"}
        if scope := self.scope or " ".join(scopes):
            data["scope"] = scope
        resp = await self._http.post(meta["token_endpoint"], data=data, auth=(self.client_id, self.client_secret))
        if resp.status_code != 200:
            raise A2AAuthError(f"token endpoint said {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        self._token = body["access_token"]
        self._expires_at = time.monotonic() + float(body.get("expires_in", 300)) - EXPIRY_SKEW
        return self._token


class AuthorizationCodePKCE:
    """Interactive user login (OAuth2 authorization-code grant + PKCE) for CLIs/desktop apps.

    Use this when a *human* is in front of the client and the token should say "this user"
    (its `sub` is the user), instead of "this agent" as with client credentials. Steps:

      1. Make a random `code_verifier` and send only its SHA-256 hash (`code_challenge`)
         in the authorize URL. Only we know the verifier, so a stolen authorization code is
         useless to anyone else. That is what lets a *public* client (no secret) be safe.
      2. Open the browser at `authorization_endpoint`; the user logs in at the IdP, so the
         client never sees their password.
      3. The IdP redirects the browser to http://127.0.0.1:<port>/callback?code=...&state=...
         where we listen for exactly one request (RFC 8252 "loopback redirect").
         `state` is a random value we check to make sure the callback belongs to *our* login.
      4. POST the `code` + `code_verifier` to `token_endpoint` -> access_token (+ refresh_token).
      5. Later, an expired token is renewed silently with the refresh_token; only if that
         fails do we ask the user to log in again.
    """

    def __init__(self, client_id: str, *, scope: str | None = None, http_client: httpx.AsyncClient | None = None,
                 open_browser: Callable[[str], Any] | None = None, login_timeout: float = 120.0,
                 announce: Callable[[str], None] = print):
        self.client_id, self.scope = client_id, scope
        self._http = http_client or httpx.AsyncClient(timeout=10)
        self._open_browser = open_browser or webbrowser.open
        self._login_timeout, self._announce = login_timeout, announce
        self._token: str | None = None
        self._expires_at = 0.0
        self._refresh_token: str | None = None
        self._lock = asyncio.Lock()  # parallel requests must not open several login tabs

    async def token(self, card: AgentCard | None, *, refresh: bool = False) -> str:
        async with self._lock:
            if self._token and not refresh and time.monotonic() < self._expires_at:
                return self._token
            if card is None:
                raise A2AAuthError("fetch the agent card first: it says where to log in")
            discovery_url, scopes = oidc_requirement(card)
            meta = await _get_json(self._http, discovery_url)
            if self._refresh_token and await self._try_refresh(meta):
                return self._token  # type: ignore[return-value]
            # `openid` makes this an OIDC login; the card's scopes say what the agent wants.
            scope = self.scope or " ".join(["openid", *scopes])
            await self._login(meta, scope)
            return self._token  # type: ignore[return-value]

    async def _try_refresh(self, meta: dict[str, Any]) -> bool:
        resp = await self._http.post(meta["token_endpoint"], data={
            "grant_type": "refresh_token", "refresh_token": self._refresh_token, "client_id": self.client_id})
        if resp.status_code != 200:  # refresh token expired/revoked: fall back to a full login
            self._refresh_token = None
            return False
        self._store(resp.json())
        return True

    async def _login(self, meta: dict[str, Any], scope: str) -> None:
        verifier, challenge = new_pkce()
        state = secrets.token_urlsafe(16)

        result: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()
        server = await asyncio.start_server(lambda r, w: _handle_callback(r, w, result), "127.0.0.1", 0)
        try:
            redirect_uri = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/callback"
            url = meta["authorization_endpoint"] + "?" + urlencode({
                "response_type": "code", "client_id": self.client_id, "redirect_uri": redirect_uri,
                "scope": scope, "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
            self._announce(f"Opening the browser to log in. If nothing opens, visit:\n  {url}")
            self._open_browser(url)
            try:
                params = await asyncio.wait_for(result, self._login_timeout)
            except asyncio.TimeoutError:
                raise A2AAuthError(f"login timed out after {self._login_timeout:.0f}s") from None
        finally:
            server.close()

        if params.get("state") != state:  # CSRF / a stray request hit our loopback port
            raise A2AAuthError("login callback has the wrong 'state'; aborting")
        if "error" in params:
            raise A2AAuthError(f"login failed: {params['error']} {params.get('error_description', '')}".strip())
        resp = await self._http.post(meta["token_endpoint"], data={
            "grant_type": "authorization_code", "code": params["code"], "redirect_uri": redirect_uri,
            "client_id": self.client_id, "code_verifier": verifier})
        if resp.status_code != 200:
            raise A2AAuthError(f"token endpoint said {resp.status_code}: {resp.text[:200]}")
        self._store(resp.json())

    def _store(self, body: dict[str, Any]) -> None:
        self._token = body["access_token"]
        self._expires_at = time.monotonic() + float(body.get("expires_in", 300)) - EXPIRY_SKEW
        self._refresh_token = body.get("refresh_token", self._refresh_token)


def new_pkce() -> tuple[str, str]:
    """(code_verifier, code_challenge): the challenge is the verifier's SHA-256, base64url (RFC 7636 S256)."""
    verifier = secrets.token_urlsafe(64)  # 86 chars, within RFC 7636's 43..128
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def _handle_callback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                           result: asyncio.Future[dict[str, str]]) -> None:
    """The one-shot web server behind the redirect_uri: parse `GET /callback?...`, show a page."""
    try:
        line = (await reader.readline()).decode("latin-1")  # e.g. "GET /callback?code=..&state=.. HTTP/1.1"
        target = urlsplit(line.split(" ")[1]) if line.count(" ") >= 2 else None
        ok = bool(target and target.path == "/callback")
        body = "<h3>Login complete.</h3>You can close this tab and return to the terminal." if ok else "not found"
        writer.write((f"HTTP/1.1 {'200 OK' if ok else '404 Not Found'}\r\nContent-Type: text/html; charset=utf-8\r\n"
                      f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}").encode())
        await writer.drain()
        if ok and not result.done():
            result.set_result({k: v[0] for k, v in parse_qs(target.query).items()})
    finally:
        writer.close()


def oidc_requirement(card: AgentCard) -> tuple[str, list[str]]:
    """(discovery URL, scopes) of the first OIDC scheme the card requires."""
    schemes = card.securitySchemes or {}
    for alternative in card.security or []:  # alternatives are ORed; pick the first we can do
        for name, scopes in alternative.items():
            scheme = schemes.get(name, {})
            if scheme.get("type") == "openIdConnect":
                return scheme["openIdConnectUrl"], scopes
    raise A2AAuthError(f"agent card requires auth we don't support: {card.security}")


def add_auth_args(parser) -> None:
    """Shared CLI flags (client CLI and demo.py). Env vars: OIDC_CLIENT_ID / OIDC_CLIENT_SECRET / A2A_TOKEN."""
    import os

    g = parser.add_argument_group("authentication (only needed if the agent card declares security)")
    g.add_argument("--client-id", default=os.getenv("OIDC_CLIENT_ID"), help="OAuth2 client id (client-credentials grant)")
    g.add_argument("--client-secret", default=os.getenv("OIDC_CLIENT_SECRET", ""), help="OAuth2 client secret")
    g.add_argument("--scope", default=None, help="override the scope taken from the agent card")
    g.add_argument("--login", action="store_true",
                   help="log in as a user in the browser (authorization code + PKCE); --client-id defaults to 'a2a-cli'")
    g.add_argument("--token", default=os.getenv("A2A_TOKEN"), help="use this bearer token as-is")


def auth_from_args(args) -> TokenSource | None:
    if args.token:
        return StaticBearer(args.token)
    if args.login:  # a human at the keyboard: public client, no secret, the user logs in at the IdP
        return AuthorizationCodePKCE(args.client_id or "a2a-cli", scope=args.scope)
    if args.client_id:
        return ClientCredentials(args.client_id, args.client_secret, scope=args.scope)
    return None
