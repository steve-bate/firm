import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import AsyncIterable

from fastapi import (
    APIRouter,
    Cookie,
    HTTPException,
    Request,
    Response,
)
from fastapi.params import Depends
from fastapi.sse import EventSourceResponse, ServerSentEvent  # FastAPI >= 0.135
from pydantic import BaseModel

from firm.core.interfaces import Identity
from firm.server.auth import get_principal
from firm.streaming.sse.notifier import InMemoryStreamNotifier, StreamNotifier
from firm.streaming.sse.store import TicketStore

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# Router factory
# -------------------------------------------------------------------

TICKET_TTL = timedelta(minutes=5)


class SSEConfig(BaseModel):
    cookie_name: str = "sse_ticket"
    cookie_path: str = "/"
    cookie_samesite: str = "lax"  # "lax" | "strict" | "none"
    cookie_secure: bool = False  # Set to True if using HTTPS
    cookie_httponly: bool = False  # Exposed to javascript?
    ticket_ttl_seconds: int = int(TICKET_TTL.total_seconds())


def create_sse_router(
    notifier: StreamNotifier = InMemoryStreamNotifier(),
    ticket_store: TicketStore | None = None,
    config: SSEConfig | None = None,
) -> APIRouter:
    """
    Create an APIRouter exposing:
      - POST /sse/control/subscribe
      - POST /sse/control/unsubscribe
      - POST /sse/control/issue-ticket
      - GET  /sse/stream

    You inject:
      - notifier: implementation of StreamNotifier
      - ticket_store: storage for SSE tickets (defaults to in-memory)
      - config: cookie / TTL config
      - get_current_user_id: your auth hook (e.g. session/JWT)
    """

    @asynccontextmanager
    async def _initialize(app):
        app.state.sse_notifier = notifier
        for tenant in app.state.tenants.values():
            path = app.url_path_for("sse_create_session")
            tenant.endpoints["streamingControl"] = f"{tenant.prefix}{path}"
        yield

    router = APIRouter(prefix="/sse", tags=["sse"], lifespan=_initialize)

    cfg = config or SSEConfig()
    store = ticket_store or TicketStore(ttl=timedelta(seconds=cfg.ticket_ttl_seconds))

    async def _get_user_id(principal: Identity | None = Depends(get_principal)) -> str:
        if principal is None:
            raise HTTPException(
                status_code=401,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Basic"},
            )
        return principal.uri

    async def _require_ticket(
        sse_ticket: str | None = Cookie(default=None, alias=cfg.cookie_name),
        user_id: str = Depends(_get_user_id),
    ) -> str:
        td = store.get_ticket(sse_ticket)
        if td is None:
            raise HTTPException(status_code=401, detail="A valid SSE session ticket is required")
        if td.user_id != user_id:
            raise HTTPException(
                status_code=401, detail="SSE ticket does not match authenticated user"
            )
        return user_id

    # --------- Models ---------

    class SubscriptionRequest(BaseModel):
        topics: list[str]

    # --------- Control endpoints ---------

    @router.get("/control/subscriptions")
    async def get_subscriptions(user_id: str = Depends(_require_ticket)) -> dict:
        logger.info("GET /sse/control/subscriptions user_id=%s", user_id)
        topics = await notifier.get_subscriptions(user_id)
        return {"topics": topics}

    @router.post("/control/subscriptions")
    async def subscribe(req: SubscriptionRequest, user_id: str = Depends(_require_ticket)) -> dict:
        logger.info("POST /control/subscriptions user_id=%s topics=%s", user_id, req.topics)
        for topic in req.topics:
            await notifier.add_subscription(user_id, topic)
        topics = await notifier.get_subscriptions(user_id)
        return {"topics": topics}

    @router.delete("/control/subscriptions", status_code=204)
    async def unsubscribe(topic: str, user_id: str = Depends(_require_ticket)) -> None:
        logger.info("DELETE /control/subscriptions user_id=%s topic=%s", user_id, topic)
        await notifier.remove_subscription(user_id, topic)

    @router.delete("/control", status_code=204)
    async def revoke_session(
        response: Response,
        sse_ticket: str | None = Cookie(default=None, alias=cfg.cookie_name),
        user_id: str = Depends(_get_user_id),
    ) -> None:
        logger.info("DELETE /control user_id=%s", user_id)
        if sse_ticket:
            store.invalidate_ticket(sse_ticket)
        response.delete_cookie(
            key=cfg.cookie_name,
            path=cfg.cookie_path,
        )

    @router.post("/control", status_code=201, name="sse_create_session")
    async def create_session(response: Response, user_id: str = Depends(_get_user_id)) -> dict:
        logger.info("POST /control user_id=%s", user_id)

        ticket, td = store.create_ticket(user_id=user_id)

        response.set_cookie(
            key=cfg.cookie_name,
            value=ticket,
            path=cfg.cookie_path,
            secure=cfg.cookie_secure,
            httponly=cfg.cookie_httponly,
            samesite=cfg.cookie_samesite,
            max_age=cfg.ticket_ttl_seconds,
        )

        prefix = router.prefix
        return {
            "subscriptions_url": f"{prefix}/control/subscriptions",
            "stream_url": f"{prefix}/stream",
            "expires_at": td.expires_at.isoformat(),
            "wildcard_support": True,
        }

    # --------- Stream endpoint ---------

    # TODO CORS considerations:
    #
    # If your SPA and SSE API are same-origin, CORS is mostly irrelevant.
    #
    # The browser already enforces same-origin reads, so you mainly rely on
    # cookie settings (Secure, HttpOnly, SameSite) and normal auth checks.
    #
    # If your SPA is on a different first-party origin (example: app.example -> api.example),
    # then CORS is required and must be strict:
    #     Access-Control-Allow-Origin must be the exact SPA origin, not *.
    #     Access-Control-Allow-Credentials: true is required if cookies are used.
    #     Vary: Origin should be set.
    #
    # Server should validate Origin against an allowlist and reject others.
    #
    # For EventSource specifically, cross-origin requests can be made, and
    # credentials can be included (withCredentials: true), so a loose
    # CORS policy could expose private stream data.

    @router.get("/stream", response_class=EventSourceResponse)
    async def sse_stream(
        request: Request,
        response: Response,
        sse_ticket: str | None = Cookie(default=None, alias=cfg.cookie_name),
        user_id: str = Depends(_get_user_id),
    ) -> AsyncIterable[ServerSentEvent]:
        logger.info("GET /sse/stream: validating ticket")
        td = store.get_ticket(sse_ticket)
        if td is None:
            logger.warning("GET /sse/stream: invalid or expired ticket")
            raise HTTPException(status_code=401, detail="Invalid or expired SSE ticket")

        # Tell nginx not to buffer the event stream.
        response.headers["X-Accel-Buffering"] = "no"

        if td.user_id != user_id:
            logger.warning(
                "GET /sse/stream: ticket user_id mismatch (ticket=%s user_id=%s)",
                td.user_id,
                user_id,
            )
            raise HTTPException(
                status_code=401, detail="SSE ticket does not match authenticated user"
            )

        user_id = td.user_id
        logger.info("GET /sse/stream: opening stream for user_id=%s", user_id)

        async for evt in notifier.notification_stream(user_id):
            yield evt

    return router
