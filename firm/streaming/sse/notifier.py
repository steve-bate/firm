# -------------------------------------------------------------------
# Abstract interface for pushing streaming notifications
# -------------------------------------------------------------------

import asyncio
import dataclasses
import logging
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping, cast

from fastapi import Request
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
    async def get_subscriptions(self, user_id: str) -> list[str]: ...

    @abstractmethod
    async def notify(self, topic: str, data: Mapping[str, Any] | object) -> None:
        """
        Publish an event to all users currently subscribed to *topic*.
        """
        ...

    @abstractmethod
    async def has_events(self, user_id: str) -> bool: ...

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


class Subscription:
    def __init__(self, topic: str):
        self.topic = topic
        self._pattern = self._parse(topic)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Subscription):
            return self.topic == other.topic
        if isinstance(other, str):
            return self.topic == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.topic)

    @staticmethod
    def _parse(t: str) -> re.Pattern | None:
        # TODO May not need full regex here
        if "+" in t or "#" in t:
            pattern_segments: list[str] = []
            subscription_segments = t.split("/")
            for n, segment in enumerate(subscription_segments):
                if segment == "+":
                    pattern_segments.append(r".*?")
                elif segment == "#":
                    if n != len(subscription_segments) - 1:
                        raise ValueError("Invalid topic pattern: '#' wildcard must be at the end")
                    pattern_segments.append(r".*")
                else:
                    if "+" in segment or "#" in segment:
                        raise ValueError("Invalid topic pattern: wildcards must be full segments")
                    else:
                        pattern_segments.append(re.escape(segment))
            pattern = re.compile(f'^{"/".join(pattern_segments)}$')
        else:
            pattern = None
        return pattern

    def is_match(self, topic: str) -> bool:
        return re.match(self._pattern, topic) is not None if self._pattern else self.topic == topic


class InMemoryStreamNotifier(StreamNotifier):
    """
    In-memory notifier backed by per-user asyncio queues.

    Call ``send_event(topic, data)`` from anywhere in the application to push
    an event to every user that is subscribed to *topic*.  Each active
    ``stream_events`` generator consumes its own queue, so events are delivered
    as soon as they are published with no polling delay.
    """

    def __init__(self) -> None:
        self._subscriptions: dict[str, dict[str, Subscription]] = {}
        # One queue per user; created lazily and torn down after streaming ends.
        self._queues: dict[str, asyncio.Queue[tuple[str, Mapping[str, Any]]]] = {}

    async def add_subscription(self, user_id: str, topic: str) -> None:
        self._subscriptions.setdefault(user_id, {})[topic] = Subscription(topic)
        logger.info("Subscribed user %s to topic %s", user_id, topic)

    async def remove_subscription(self, user_id: str, topic: str) -> None:
        self._subscriptions.setdefault(user_id, {}).pop(topic, None)
        logger.info("Unsubscribed user %s from topic %s", user_id, topic)

    async def has_subscriptions(self, user_id: str) -> bool:
        result = bool(self._subscriptions.get(user_id))
        logger.info("has_subscriptions: user_id=%s result=%s", user_id, result)
        return result

    # For testing
    async def has_events(self, user_id: str) -> bool:
        q = self._queues.get(user_id)
        result = q is not None and not q.empty()
        logger.info("has_events: user_id=%s result=%s", user_id, result)
        return result

    async def get_subscriptions(self, user_id: str) -> list[str]:
        return sorted(list(self._subscriptions.get(user_id, {}).keys()))

    async def notify(self, topic: str, data: Mapping[str, Any] | object) -> None:
        """Publish *data* to every user subscribed to *topic*."""
        if dataclasses.is_dataclass(data):
            payload: Mapping[str, Any] = dataclasses.asdict(data)  # type: ignore
        else:
            payload = cast(Mapping[str, Any], data)
        for user_id, subscribed_topics in self._subscriptions.items():
            matched = [
                subscribed_topic.is_match(topic) for subscribed_topic in subscribed_topics.values()
            ]
            if any(matched):
                q = self._queues.setdefault(user_id, asyncio.Queue())
                await q.put((topic, payload))
                logger.info("send_event: queued event for user_id=%s topic=%s", user_id, topic)

    async def notification_stream(self, user_id: str) -> AsyncIterator[ServerSentEvent]:
        logger.info("stream_events: starting stream for user_id=%s", user_id)
        q = self._queues.setdefault(user_id, asyncio.Queue())
        try:
            while True:
                topic, data = await q.get()
                logger.info("update event for user_id=%s topic=%s", user_id, topic)
                id_ = data.get("id") if isinstance(data, dict) else uuid.uuid4().hex
                yield ServerSentEvent(id=id_, event="notification", data=dict(data))
        except (asyncio.CancelledError, GeneratorExit):
            logger.info("stream_events: stream closed for user_id=%s", user_id)
            raise
        finally:
            self._queues.pop(user_id, None)


@dataclass
class StreamEvent:
    id: str
    topic: str
    type: str  # TODO replace with enum
    published: str
    payload: Mapping[str, Any]


# Dependency Injection Support


def get_notifier(request: Request) -> StreamNotifier:
    return request.app.state.sse_notifier
