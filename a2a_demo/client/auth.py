"""Client-side auth: learn from the Agent Card that OIDC is needed, then get a token.

Flow (OAuth2 client-credentials, i.e. agent-to-agent, no human involved):
  1. Agent Card  -> securitySchemes.<name>.openIdConnectUrl, security[].<name> = scopes
  2. GET openIdConnectUrl (OIDC discovery) -> token_endpoint
  3. POST token_endpoint grant_type=client_credentials -> access_token (JWT)
  4. every JSON-RPC request carries  Authorization: Bearer <access_token>
"""

from __future__ import annotations

import time
from typing import Any, Protocol

import httpx

from ..protocol import AgentCard

EXPIRY_SKEW = 30  # refresh a little before the token really expires


class A2AAuthError(Exception):
    """The agent wants credentials we don't have (or rejected the ones we sent)."""


class TokenSource(Protocol):
    async def token(self, card: AgentCard | None, *, refresh: bool = False) -> str: ...


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
        meta = (await self._get_json(discovery_url))
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

    async def _get_json(self, url: str) -> dict[str, Any]:
        r = await self._http.get(url)
        r.raise_for_status()
        return r.json()


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
    g.add_argument("--token", default=os.getenv("A2A_TOKEN"), help="use this bearer token as-is")


def auth_from_args(args) -> TokenSource | None:
    if args.token:
        return StaticBearer(args.token)
    if args.client_id:
        return ClientCredentials(args.client_id, args.client_secret, scope=args.scope)
    return None
