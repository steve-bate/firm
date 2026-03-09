import logging
from datetime import timedelta
from typing import AsyncIterable, Callable

from fastapi import (
    APIRouter,
    Cookie,
    HTTPException,
    Request,
    Response,
)
from fastapi.sse import EventSourceResponse, ServerSentEvent  # FastAPI >= 0.135
from pydantic import BaseModel

from firm.streaming.notifier import StreamNotifier
from firm.streaming.store import TicketStore

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
    notifier: StreamNotifier,
    ticket_store: TicketStore | None = None,
    config: SSEConfig | None = None,
    get_current_user_id: Callable[[], str] | None = None,
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
    router = APIRouter(prefix="/sse", tags=["sse"])

    cfg = config or SSEConfig()
    store = ticket_store or TicketStore(ttl=timedelta(seconds=cfg.ticket_ttl_seconds))

    # Fallback auth if caller doesn't provide one
    def _default_get_current_user_id() -> str:
        # In real use, DI a function that reads session/JWT
        return "user-123"

    _get_user_id = get_current_user_id or _default_get_current_user_id

    # --------- Models ---------

    class SubscriptionRequest(BaseModel):
        topic: str

    # --------- Control endpoints ---------

    @router.post("/control/subscribe")
    async def subscribe(req: SubscriptionRequest) -> dict:
        user_id = _get_user_id()
        logger.info("POST /sse/control/subscribe user_id=%s topic=%s", user_id, req.topic)
        await notifier.add_subscription(user_id, req.topic)
        return {"status": "ok"}

    @router.post("/control/unsubscribe")
    async def unsubscribe(req: SubscriptionRequest) -> dict:
        user_id = _get_user_id()
        logger.info("POST /sse/control/unsubscribe user_id=%s topic=%s", user_id, req.topic)
        await notifier.remove_subscription(user_id, req.topic)
        return {"status": "ok"}

    @router.post("/control/issue-ticket")
    async def issue_ticket(response: Response) -> dict:
        user_id = _get_user_id()
        logger.info("POST /sse/control/issue-ticket user_id=%s", user_id)
        if not await notifier.has_subscriptions(user_id):
            logger.warning("issue_ticket: user_id=%s has no active subscriptions", user_id)
            raise HTTPException(status_code=400, detail="No active subscriptions")

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
        return {"status": "ok", "ticket": ticket, "expires_at": td.expires_at.isoformat()}

    # --------- Stream endpoint ---------

    @router.get("/stream", response_class=EventSourceResponse)
    async def sse_stream(
        request: Request,
        response: Response,
        sse_ticket: str | None = Cookie(default=None, alias=cfg.cookie_name),
    ) -> AsyncIterable[ServerSentEvent]:
        logger.info("GET /sse/stream: validating ticket")
        td = store.get_ticket(sse_ticket)
        if td is None:
            logger.warning("GET /sse/stream: invalid or expired ticket")
            raise HTTPException(status_code=401, detail="Invalid or expired SSE ticket")

        # Tell nginx not to buffer the event stream.
        response.headers["X-Accel-Buffering"] = "no"

        user_id = td.user_id
        logger.info("GET /sse/stream: opening stream for user_id=%s", user_id)

        async for evt in notifier.notification_stream(user_id):
            yield evt

    return router
