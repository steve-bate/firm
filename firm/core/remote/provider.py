"""A provider of documents held by other servers.

Shared by every tenant - a remote document is the same bytes for all of them -
and it answers one question: ``get(uri)``, which returns a cached copy or fetches
one. It is deliberately not a ``ResourceStore``: it is not a source of record, so
it offers nothing that would imply one - no ``query``, no ``put``, no
enumeration into a search result or a collection count.

Fetch policy lives here. Persistence lives behind ``RemoteCache``, which stores
finished entries verbatim.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable, cast
from urllib.parse import urldefrag

from firm.core.interfaces import (
    DEFAULT_HTTP_TIMEOUT,
    HttpResponse,
    HttpTransport,
    JSONObject,
)
from firm.core.remote.cache import CacheEntry, RemoteCache

log = logging.getLogger(__name__)

ACTIVITYPUB_ACCEPT = (
    "application/activity+json, "
    'application/ld+json; profile="https://www.w3.org/ns/activitystreams", '
    "application/json;q=0.5"
)

GONE_STATUSES = frozenset({404, 410})


class FetchError(Exception):
    """A target we must not fetch, or an answer that must not be cached as content."""


def cache_key(uri: str) -> str:
    """The cache key for a URI: the fragment never reaches the origin."""
    return urldefrag(uri)[0]


@dataclass(frozen=True)
class FetchPolicy:
    """Timings and limits for outbound fetches. No storage knowledge."""

    ttl: float = 300.0
    failure_ttl: float = 60.0
    not_found_ttl: float = 86_400.0
    timeout: float = DEFAULT_HTTP_TIMEOUT
    accept: str = ACTIVITYPUB_ACCEPT


class RemoteResourceProvider:
    """Supplies documents from other servers, cached.

    Read-only and never authoritative: a caller must not use it to decide what
    this server holds, only what some other server is willing to show.
    """

    def __init__(
        self,
        cache: RemoteCache,
        transport: HttpTransport,
        *,
        policy: FetchPolicy | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._cache = cache
        self._transport = transport
        self._policy = policy or FetchPolicy()
        self._clock = clock

    def __repr__(self) -> str:
        return f"RemoteResourceProvider({self._policy!r})"

    @property
    def policy(self) -> FetchPolicy:
        return self._policy

    async def get(self, uri: str) -> JSONObject | None:
        """Return the cached copy, or fetch one. ``None`` if it cannot be had."""
        key = cache_key(uri)
        if not key:
            return None

        entry = await self._cache.get(key)
        if entry is not None and entry.is_fresh(self._clock()):
            return entry.document

        return await self._fetch_and_cache(key)

    async def _fetch_and_cache(self, key: str) -> JSONObject | None:
        now = self._clock()
        document: JSONObject | None = None
        status_code: int | None = None
        try:
            status_code, document = await self._fetch(key)
        except FetchError as exc:
            log.info("Refusing to fetch %s: %s", key, exc)
        except Exception as exc:  # a transport failure is a failed fetch, not a bug here
            log.warning("Fetch failed for %s: %s", key, exc)

        if document is not None:
            ttl = self._policy.ttl
        elif status_code in GONE_STATUSES:
            ttl = self._policy.not_found_ttl
        else:
            ttl = self._policy.failure_ttl

        await self._cache.put(
            CacheEntry(
                uri=key,
                document=document,
                status_code=status_code,
                fetched_at=now,
                expires_at=now + ttl,
            )
        )
        return document

    async def _fetch(self, uri: str) -> tuple[int, JSONObject | None]:
        response = await self._transport.get(
            uri,
            headers={"Accept": self._policy.accept},
            follow_redirects=False,
            timeout=self._policy.timeout,
        )
        if response.status_code != 200:
            return response.status_code, None
        return 200, self._parse(uri, response)

    def _parse(self, uri: str, response: HttpResponse) -> JSONObject:
        body = response.body or b""
        try:
            document = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise FetchError(f"Invalid JSON from {uri}: {exc}") from exc
        if not isinstance(document, dict):
            raise FetchError(f"Expected a JSON object from {uri}")
        return cast(JSONObject, document)
