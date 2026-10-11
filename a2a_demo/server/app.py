"""HTTP layer: Agent Card endpoint + JSON-RPC 2.0 endpoint (+ SSE for streaming)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from ..config import Settings, load_settings
from ..protocol import (
    AGENT_CARD_PATH,
    A2AError,
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    ErrorCode,
    MessageSendParams,
    OpenIdConnectSecurityScheme,
    TaskIdParams,
    TaskQueryParams,
    rpc_error,
    rpc_result,
)
from .auth import AuthError, OIDCVerifier, discovery_url
from .handler import A2AHandler
from .llm import ChatBackend, OpenRouterChat

log = logging.getLogger("a2a.server")


def build_agent_card(settings: Settings) -> AgentCard:
    secured = {}
    if settings.oidc_issuer:
        # This is how a client learns it needs OIDC: the card (public) points at the IdP.
        secured = dict(
            securitySchemes={
                "oidc": OpenIdConnectSecurityScheme(
                    description="OAuth2 access token from the OIDC provider, sent as 'Authorization: Bearer'.",
                    openIdConnectUrl=discovery_url(settings.oidc_issuer),
                ).to_wire()
            },
            security=[{"oidc": [settings.oidc_scope]}],
        )
    return AgentCard(
        name="OpenRouter Chat Agent",
        description=f"A general-purpose chat agent backed by '{settings.model}' on OpenRouter.",
        url=settings.public_url,
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=True, pushNotifications=False),
        skills=[
            AgentSkill(
                id="chat",
                name="General chat",
                description="Have a multi-turn conversation. Reuse contextId to keep the memory.",
                tags=["chat", "qa"],
                examples=["Explain the A2A protocol in two sentences.", "Tell me a joke."],
            ),
            AgentSkill(
                id="echo",
                name="Echo / ping (no LLM)",
                description="'/ping' replies 'pong'; '/echo <text>' replies with <text>. "
                "Answered locally, so it works without an LLM backend.",
                tags=["test", "echo"],
                examples=["/ping", "/echo hello"],
            ),
        ],
        **secured,
    )


def sse(payload: dict[str, Any]) -> str:
    """One Server-Sent Event: every event carries a full JSON-RPC response."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def create_app(
    settings: Settings | None = None,
    llm: ChatBackend | None = None,
    idp_http: httpx.AsyncClient | None = None,
) -> FastAPI:
    """`idp_http` is the HTTP client used to reach the OIDC provider (injectable for tests)."""
    settings = settings or load_settings()
    verifier = OIDCVerifier(settings, idp_http) if settings.oidc_issuer else None
    handler = A2AHandler(llm or OpenRouterChat(settings))
    card = build_agent_card(settings)
    app = FastAPI(title=card.name)

    # ---- discovery ------------------------------------------------------
    @app.get(AGENT_CARD_PATH)
    @app.get("/.well-known/agent.json")  # name used by older spec versions
    async def agent_card() -> JSONResponse:
        return JSONResponse(card.to_wire())

    # ---- auth -----------------------------------------------------------
    @app.exception_handler(AuthError)
    async def auth_error(_: Request, err: AuthError) -> JSONResponse:
        # Auth failures are HTTP-level (401/403), unlike JSON-RPC errors which are HTTP 200.
        return JSONResponse(
            {"error": err.error, "error_description": err.description},
            status_code=err.status,
            headers={"WWW-Authenticate": err.www_authenticate},
        )

    async def require_auth(request: Request) -> None:
        if verifier:
            claims = await verifier.verify(request.headers.get("authorization"))
            log.info("authenticated sub=%s", claims.get("sub"))

    # ---- JSON-RPC -------------------------------------------------------
    @app.post("/", dependencies=[Depends(require_auth)])
    async def jsonrpc(request: Request):
        req_id: Any = None
        try:
            try:
                body = await request.json()
            except ValueError:
                raise A2AError(ErrorCode.PARSE_ERROR, "Invalid JSON payload")

            if not isinstance(body, dict):
                raise A2AError(ErrorCode.INVALID_REQUEST, "Request must be a JSON object")
            req_id = body.get("id")
            method = body.get("method")
            if body.get("jsonrpc") != "2.0" or not isinstance(method, str):
                raise A2AError(ErrorCode.INVALID_REQUEST, "Not a valid JSON-RPC 2.0 request")
            raw_params = body.get("params") or {}
            log.info("<- %s (id=%s)", method, req_id)

            try:
                if method == "message/send":
                    result = await handler.message_send(MessageSendParams.model_validate(raw_params))
                    return JSONResponse(rpc_result(req_id, result))

                if method == "message/stream":
                    first, events = await handler.message_stream(MessageSendParams.model_validate(raw_params))

                    async def stream() -> AsyncIterator[str]:
                        yield sse(rpc_result(req_id, first))  # 1st event: the Task itself
                        async for ev in events:
                            yield sse(rpc_result(req_id, ev))

                    return StreamingResponse(
                        stream(),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                    )

                if method == "tasks/get":
                    result = await handler.tasks_get(TaskQueryParams.model_validate(raw_params))
                    return JSONResponse(rpc_result(req_id, result))

                if method == "tasks/cancel":
                    result = await handler.tasks_cancel(TaskIdParams.model_validate(raw_params))
                    return JSONResponse(rpc_result(req_id, result))
            except ValidationError as ve:
                raise A2AError(ErrorCode.INVALID_PARAMS, "Invalid params", json.loads(ve.json(include_url=False)))

            raise A2AError(ErrorCode.METHOD_NOT_FOUND, f"Method not found: {method}")

        except A2AError as err:
            # JSON-RPC errors are still HTTP 200: the transport worked, the call failed.
            return JSONResponse(rpc_error(req_id, err))
        except Exception as exc:
            log.exception("unhandled error")
            return JSONResponse(rpc_error(req_id, A2AError(ErrorCode.INTERNAL_ERROR, str(exc))))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s")
    settings = load_settings()
    print(f"A2A agent card: http://{settings.host}:{settings.port}{AGENT_CARD_PATH}")
    print(f"auth: OIDC issuer {settings.oidc_issuer}" if settings.oidc_issuer else "auth: disabled (set OIDC_ISSUER)")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
