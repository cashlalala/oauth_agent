"""Web-side login: the browser user logs in at the IdP, the web *server* holds the tokens.

This is the "backend for frontend" pattern. The browser only ever sees an opaque, HttpOnly
session cookie; the access/refresh tokens stay in `WebSession` on the server, which calls the
agent on the user's behalf (so the token's `sub` is the user, exactly like `client --login`).
It is the same authorization-code + PKCE flow as `AuthorizationCodePKCE`, except the
redirect_uri is a route of this web app instead of a loopback port.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from ..client.auth import EXPIRY_SKEW, A2AAuthError, new_pkce, oidc_requirement
from ..config import WebSettings
from ..protocol import AgentCard


@dataclass
class WebSession:
    """Server-side state of one browser (found via the session cookie). Also a `TokenSource`."""

    sid: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    # pending login (set by /auth/login, consumed by /auth/callback)
    state: str | None = None
    verifier: str | None = None
    # tokens (set after a successful login)
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: float = 0.0
    user: dict[str, Any] | None = None
    contexts: set[str] = field(default_factory=set)  # conversations this browser started
    tasks: set[str] = field(default_factory=set)
    login: WebLogin | None = None

    async def token(self, card: AgentCard | None, *, refresh: bool = False) -> str:
        if self.access_token and not refresh and time.monotonic() < self.expires_at:
            return self.access_token
        if card is None or not self.login or not await self.login.refresh(self, card):
            self.access_token = None
            raise A2AAuthError("session expired: log in again")
        return self.access_token  # type: ignore[return-value]


class WebLogin:
    def __init__(self, settings: WebSettings, http: httpx.AsyncClient):
        self.settings, self._http = settings, http

    async def _discovery(self, card: AgentCard) -> tuple[dict[str, Any], list[str]]:
        url, scopes = oidc_requirement(card)
        r = await self._http.get(url)
        r.raise_for_status()
        return r.json(), scopes

    async def start(self, session: WebSession, card: AgentCard) -> str:
        """Return the IdP URL to send the browser to."""
        meta, scopes = await self._discovery(card)
        verifier, challenge = new_pkce()
        session.verifier, session.state = verifier, secrets.token_urlsafe(16)
        return meta["authorization_endpoint"] + "?" + urlencode({
            "response_type": "code", "client_id": self.settings.client_id,
            "redirect_uri": self.settings.redirect_uri, "scope": " ".join(["openid", *scopes]),
            "state": session.state, "code_challenge": challenge, "code_challenge_method": "S256"})

    async def finish(self, session: WebSession, card: AgentCard, params: dict[str, str]) -> None:
        """Handle the redirect back from the IdP: check `state`, trade the code for tokens."""
        state, verifier = session.state, session.verifier
        session.state = session.verifier = None  # single use, whatever happens next
        if not state or params.get("state") != state:  # forged callback / login started in another browser
            raise A2AAuthError("login callback has the wrong 'state'")
        if "error" in params:
            raise A2AAuthError(f"login failed: {params['error']} {params.get('error_description', '')}".strip())
        meta, _ = await self._discovery(card)
        resp = await self._http.post(meta["token_endpoint"], data={
            "grant_type": "authorization_code", "code": params.get("code", ""),
            "redirect_uri": self.settings.redirect_uri, "client_id": self.settings.client_id,
            "code_verifier": verifier})
        if resp.status_code != 200:
            raise A2AAuthError(f"token endpoint said {resp.status_code}: {resp.text[:200]}")
        self._store(session, resp.json())
        session.login = self

    async def refresh(self, session: WebSession, card: AgentCard) -> bool:
        if not session.refresh_token:
            return False
        meta, _ = await self._discovery(card)
        resp = await self._http.post(meta["token_endpoint"], data={
            "grant_type": "refresh_token", "refresh_token": session.refresh_token,
            "client_id": self.settings.client_id})
        if resp.status_code != 200:
            session.refresh_token = None
            return False
        self._store(session, resp.json())
        return True

    @staticmethod
    def _store(session: WebSession, body: dict[str, Any]) -> None:
        session.access_token = body["access_token"]
        session.expires_at = time.monotonic() + float(body.get("expires_in", 300)) - EXPIRY_SKEW
        session.refresh_token = body.get("refresh_token", session.refresh_token)
        # The id_token came straight from the token endpoint over the back channel, so for
        # *display* we may read it without checking the signature (OIDC Core 3.1.3.7).
        # Authorization is still done by the agent, which verifies the access token properly.
        raw = body.get("id_token") or body["access_token"]
        claims = jwt.decode(raw, options={"verify_signature": False})
        session.user = {"sub": claims.get("sub"), "name": claims.get("name") or claims.get("preferred_username")}
