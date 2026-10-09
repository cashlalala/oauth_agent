"""OIDC bearer-token validation for the JSON-RPC endpoint.

A2A keeps auth out of the protocol: it is plain HTTP auth. The Agent Card *declares* the
scheme (`securitySchemes` / `security`), the client gets a token from the identity provider
by itself, and sends `Authorization: Bearer <jwt>` with every request. This module is the
server half: fetch the IdP's signing keys (JWKS) and check signature, issuer, audience,
expiry and scope.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import jwt  # PyJWT

from ..config import Settings

log = logging.getLogger("a2a.auth")

ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]  # asymmetric only, never HS*
JWKS_MIN_REFRESH_SECONDS = 30  # don't let unknown `kid`s turn into a request storm on the IdP


class AuthError(Exception):
    """Maps to an HTTP error (not a JSON-RPC error): auth happens before JSON-RPC parsing."""

    def __init__(self, status: int, error: str, description: str):
        super().__init__(description)
        self.status, self.error, self.description = status, error, description

    @property
    def www_authenticate(self) -> str:
        return f'Bearer error="{self.error}", error_description="{self.description}"'


def discovery_url(issuer: str) -> str:
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


class OIDCVerifier:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None):
        self.issuer = settings.oidc_issuer
        self.audience = settings.oidc_audience
        self.required_scope = settings.oidc_scope
        self._http = http_client or httpx.AsyncClient(timeout=10)
        self._keys: dict[str, Any] = {}  # kid -> public key
        self._jwks_fetched_at = float("-inf")

    async def _refresh_keys(self) -> None:
        """OIDC discovery -> jwks_uri -> signing keys."""
        r = await self._http.get(discovery_url(self.issuer))
        r.raise_for_status()
        meta = r.json()
        if meta.get("issuer") != self.issuer:  # guards against a misconfigured/hostile issuer URL
            raise RuntimeError(f"discovery issuer {meta.get('issuer')!r} != configured {self.issuer!r}")
        r = await self._http.get(meta["jwks_uri"])
        r.raise_for_status()
        self._keys = {
            k["kid"]: jwt.PyJWK(k).key for k in r.json()["keys"] if k.get("use", "sig") == "sig" and "kid" in k
        }
        self._jwks_fetched_at = time.monotonic()
        log.info("loaded %d signing key(s) from %s", len(self._keys), self.issuer)

    async def _key_for(self, kid: str) -> Any:
        if kid not in self._keys and time.monotonic() - self._jwks_fetched_at > JWKS_MIN_REFRESH_SECONDS:
            try:
                await self._refresh_keys()  # first use, or the IdP rotated its keys
            except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as e:
                log.error("cannot load JWKS: %s", e)
                raise AuthError(503, "temporarily_unavailable", "identity provider unreachable") from e
        if kid not in self._keys:
            raise AuthError(401, "invalid_token", "unknown signing key")
        return self._keys[kid]

    async def verify(self, authorization: str | None) -> dict[str, Any]:
        """Return the validated claims, or raise AuthError (401 / 403 / 503)."""
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthError(401, "invalid_request", "missing bearer token")
        token = token.strip()
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            if not kid:
                raise AuthError(401, "invalid_token", "token has no kid")
            claims = jwt.decode(
                token,
                await self._key_for(kid),
                algorithms=ALGORITHMS,
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.InvalidTokenError as e:  # bad signature, expired, wrong iss/aud, malformed, ...
            raise AuthError(401, "invalid_token", str(e)) from e

        if self.required_scope and self.required_scope not in token_scopes(claims):
            raise AuthError(403, "insufficient_scope", f"scope '{self.required_scope}' required")
        return claims


def token_scopes(claims: dict[str, Any]) -> set[str]:
    """Scopes live in `scope` (space-separated, RFC 9068) or `scp` (string/list, Entra & others)."""
    out: set[str] = set()
    for name in ("scope", "scp"):
        v = claims.get(name)
        if isinstance(v, str):
            out.update(v.split())
        elif isinstance(v, list):
            out.update(map(str, v))
    return out
