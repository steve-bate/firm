# -------------------------------------------------------------------
# Abstract interface for pushing streaming notifications
# -------------------------------------------------------------------

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Dict, Mapping, Set

from fastapi.sse import ServerSentEvent

logger = logging.getLogger(__name__)


class StreamNotifier(ABC):
    """
    Abstract interface for pushing events into user streams.

    Implementations are responsible for:
    - Keeping per-user subscription state (topics, filters, etc.)
    - Providing an async iterator that yields ServerSentEvent objects
      for a given user (multiplexed across that user's subscriptions).
    """

    @abstractmethod
    async def add_subscription(self, user_id: str, topic: str) -> None: ...

    @abstractmethod
    async def remove_subscription(self, user_id: str, topic: str) -> None: ...

    @abstractmethod
    async def has_subscriptions(self, user_id: str) -> bool: ...

    @abstractmethod
    async def notify(self, topic: str, data: Mapping[str, Any]) -> None:
        """
        Publish an event to all users currently subscribed to *topic*.
        """
        ...

    @abstractmethod
    def notification_stream(self, user_id: str) -> AsyncIterator[ServerSentEvent]:
        """
        Return an async iterator that yields ServerSentEvent for this user.

        Implementations should:
        - Deliver events published via notify() for topics the user is subscribed to.
        - Stop when the client disconnects (FastAPI signals this via GeneratorExit /
          CancelledError on the underlying async generator).
        """
        ...


# -------------------------------------------------------------------
# Simple in-memory implementation (example)
# -------------------------------------------------------------------


class InMemoryStreamNotifier(StreamNotifier):
    """
    In-memory notifier backed by per-user asyncio queues.

    Call ``send_event(topic, data)`` from anywhere in the application to push
    an event to every user that is subscribed to *topic*.  Each active
    ``stream_events`` generator consumes its own queue, so events are delivered
    as soon as they are published with no polling delay.
    """

    def __init__(self) -> None:
        self._subscriptions: Dict[str, Set[str]] = {}
        # One queue per user; created lazily and torn down after streaming ends.
        self._queues: Dict[str, asyncio.Queue[tuple[str, Mapping[str, Any]]]] = {}

    async def add_subscription(self, user_id: str, topic: str) -> None:
        self._subscriptions.setdefault(user_id, set()).add(topic)
        logger.info("Subscribed user %s to topic %s", user_id, topic)

    async def remove_subscription(self, user_id: str, topic: str) -> None:
        self._subscriptions.setdefault(user_id, set()).discard(topic)
        logger.info("Unsubscribed user %s from topic %s", user_id, topic)

    async def has_subscriptions(self, user_id: str) -> bool:
        result = bool(self._subscriptions.get(user_id))
        logger.info("has_subscriptions: user_id=%s result=%s", user_id, result)
        return result

    async def notify(self, topic: str, data: Mapping[str, Any]) -> None:
        """Publish *data* to every user subscribed to *topic*."""
        for user_id, topics in self._subscriptions.items():
            if topic in topics or any(re.match(t, topic) for t in topics):
                q = self._queues.setdefault(user_id, asyncio.Queue())
                await q.put((topic, data))
                logger.info("send_event: queued event for user_id=%s topic=%s", user_id, topic)

    async def notification_stream(self, user_id: str) -> AsyncIterator[ServerSentEvent]:
        logger.info("stream_events: starting stream for user_id=%s", user_id)
        q: asyncio.Queue[tuple[str, Mapping[str, Any]]] = asyncio.Queue()
        self._queues[user_id] = q
        try:
            while True:
                topic, data = await q.get()
                logger.info("update event for user_id=%s topic=%s", user_id, topic)
                yield ServerSentEvent(event="update", topic=topic, data=dict(data))
        except (asyncio.CancelledError, GeneratorExit):
            logger.info("stream_events: stream closed for user_id=%s", user_id)
            raise
        finally:
            self._queues.pop(user_id, None)
