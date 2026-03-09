import secrets
from datetime import datetime, timedelta
from typing import Dict

from pydantic import BaseModel

from firm.streaming.router import logger


class TicketData(BaseModel):
    user_id: str
    expires_at: datetime


class TicketStore:
    """
    Abstractable ticket store.
    For simplicity, this example uses an in-memory dict.
    """

    def __init__(self, ttl: timedelta) -> None:
        self._ttl = ttl
        self._tickets: Dict[str, TicketData] = {}

    def create_ticket(self, user_id: str) -> tuple[str, TicketData]:
        ticket = secrets.token_urlsafe(32)
        td = TicketData(
            user_id=user_id,
            expires_at=datetime.utcnow() + self._ttl,
        )
        self._tickets[ticket] = td
        logger.info(
            "create_ticket: issued ticket for user_id=%s expires_at=%s",
            user_id,
            td.expires_at.isoformat(),
        )
        return ticket, td

    def get_ticket(self, ticket: str | None) -> TicketData | None:
        if not ticket:
            logger.debug("get_ticket: no ticket provided")
            return None
        td = self._tickets.get(ticket)
        if not td:
            logger.warning("get_ticket: unknown ticket")
            return None
        if td.expires_at < datetime.utcnow():
            logger.warning("get_ticket: expired ticket for user_id=%s", td.user_id)
            self._tickets.pop(ticket, None)
            return None
        logger.debug("get_ticket: valid ticket for user_id=%s", td.user_id)
        return td

    def invalidate_ticket(self, ticket: str) -> None:
        logger.info("invalidate_ticket: removing ticket")
        self._tickets.pop(ticket, None)
