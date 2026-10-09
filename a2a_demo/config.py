"""Settings loaded from environment variables / .env (the API key never lives in code)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    host: str
    port: int
    # OIDC protection of the JSON-RPC endpoint. Empty issuer = auth disabled (open agent).
    oidc_issuer: str = ""
    oidc_audience: str = "a2a-agent"
    oidc_scope: str = "a2a.invoke"  # scope the access token must carry

    @property
    def public_url(self) -> str:
        return f"http://{self.host}:{self.port}/"


def load_settings() -> Settings:
    return Settings(
        api_key=os.getenv("OPENROUTER_API_KEY", ""),
        base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        model=os.getenv("OPENROUTER_MODEL", "openrouter/free"),
        host=os.getenv("A2A_SERVER_HOST", "127.0.0.1"),
        port=int(os.getenv("A2A_SERVER_PORT", "9999")),
        oidc_issuer=os.getenv("OIDC_ISSUER", "").rstrip("/"),
        oidc_audience=os.getenv("OIDC_AUDIENCE", "a2a-agent"),
        oidc_scope=os.getenv("OIDC_SCOPE", "a2a.invoke"),
    )
