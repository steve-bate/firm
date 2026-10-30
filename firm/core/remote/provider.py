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

import asyncio
import ipaddress
import json
import logging
import socket
import time
from dataclasses import dataclass
from typing import Callable, cast
from urllib.parse import urldefrag, urljoin, urlparse

from firm.core.interfaces import (
    DEFAULT_HTTP_TIMEOUT,
    HttpRequestSigner,
    HttpResponse,
    HttpTransport,
    JSONObject,
)
from firm.core.remote.cache import DEFAULT_MAX_ENTRIES, CacheEntry, RemoteCache

log = logging.getLogger(__name__)

ACTIVITYPUB_ACCEPT = (
    "application/activity+json, "
    'application/ld+json; profile="https://www.w3.org/ns/activitystreams", '
    "application/json;q=0.5"
)

# What a response may be declared as. The profile parameter is not required: many
# servers omit it, and the body is validated against the requested id either way.
ACTIVITYPUB_MEDIA_TYPES = frozenset(
    {"application/activity+json", "application/ld+json", "application/json"}
)

GONE_STATUSES = frozenset({404, 410})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 3

BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")


class FetchError(Exception):
    """A target we must not fetch, or an answer that must not be cached as content."""


def cache_key(uri: str) -> str:
    """The cache key for a URI: the fragment never reaches the origin."""
    return urldefrag(uri)[0]


def _resolve_host(host: str, port: int) -> list[str]:
    """Every address a host names. Patched in tests."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _address_is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def assert_fetchable(uri: str, *, allow_private_addresses: bool = False) -> str:
    """Validate a target before any connection; return it without its fragment.

    Every resolved address must be globally routable. One private answer is enough
    to refuse the name outright, rather than filtering to the public addresses:
    a name that points inward is not a name this server should be using.
    """
    base, _fragment = urldefrag(uri)
    parts = urlparse(base)
    if parts.scheme not in ("http", "https"):
        raise FetchError(f"Unsupported scheme: {parts.scheme!r}")
    if not parts.hostname:
        raise FetchError(f"No host in URI: {uri}")
    if parts.username or parts.password:
        raise FetchError(f"Credentials in URI are not allowed: {uri}")
    if allow_private_addresses:
        return base

    host = parts.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise FetchError(f"Refusing non-public host: {host}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = await asyncio.to_thread(_resolve_host, host, port)
    except (socket.gaierror, OSError) as exc:
        raise FetchError(f"Cannot resolve {host}: {exc}") from exc
    if not addresses:
        raise FetchError(f"Cannot resolve {host}")
    blocked = [address for address in addresses if not _address_is_public(address)]
    if blocked:
        raise FetchError(f"Refusing non-public address for {host}: {blocked[0]}")
    return base


@dataclass(frozen=True)
class FetchPolicy:
    """Timings and limits for outbound fetches. No storage knowledge."""

    ttl: float = 300.0
    failure_ttl: float = 60.0
    not_found_ttl: float = 86_400.0
    max_bytes: int = 1_048_576
    max_entries: int = DEFAULT_MAX_ENTRIES
    allow_private_addresses: bool = False
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
        signer: HttpRequestSigner | None = None,
    ) -> None:
        self._cache = cache
        self._transport = transport
        self._policy = policy or FetchPolicy()
        self._clock = clock
        self._signer = signer
        self._inflight: dict[str, asyncio.Task[JSONObject | None]] = {}

    def set_signer(self, signer: HttpRequestSigner | None) -> None:
        """Attach a signer once the tenants - and so the key it names - exist."""
        self._signer = signer

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

        now = self._clock()
        stale: JSONObject | None = None
        entry = await self._cache.get(key)
        if entry is not None:
            if entry.is_fresh(now):
                return entry.document
            if entry.document is not None:
                stale = entry.document

        if (task := self._inflight.get(key)) is not None:
            return await asyncio.shield(task)

        task = asyncio.create_task(self._fetch_and_cache(key, stale))
        self._inflight[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            self._inflight.pop(key, None)

    async def invalidate(self, uri: str) -> None:
        """Forget the cached copy. The origin still has the document."""
        await self._cache.delete(cache_key(uri))

    async def purge(self) -> int:
        """Drop expired entries. Returns how many went."""
        return await self._cache.purge_expired(self._clock())

    async def close(self) -> None:
        for task in list(self._inflight.values()):
            task.cancel()
        self._inflight.clear()
        await self._cache.close()

    async def _fetch_and_cache(
        self, key: str, stale: JSONObject | None = None
    ) -> JSONObject | None:
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
            document = stale

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
        target = uri
        for _ in range(MAX_REDIRECTS + 1):
            target = await assert_fetchable(
                target, allow_private_addresses=self._policy.allow_private_addresses
            )
            response = await self._transport.get(
                target,
                headers={"Accept": self._policy.accept},
                auth=self._signer,
                follow_redirects=False,
                timeout=self._policy.timeout,
            )
            if response.status_code in REDIRECT_STATUSES:
                location = response.headers.get("Location") or response.headers.get("location")
                if not location:
                    raise FetchError(f"Redirect without Location from {target}")
                target = urljoin(target, location)
                continue
            if response.status_code != 200:
                return response.status_code, None
            return 200, self._parse(target, response)
        raise FetchError(f"Too many redirects for {uri}")

    @staticmethod
    def _check_media_type(uri: str, response: HttpResponse) -> None:
        """Refuse a body that was not served as ActivityPub.

        A missing Content-Type is tolerated: the header cannot then contradict the
        body, and the ``id`` rule below still has to hold.
        """
        declared = response.media_type
        if declared is None:
            return
        media_type = declared.split(";", 1)[0].strip().lower()
        if media_type not in ACTIVITYPUB_MEDIA_TYPES:
            raise FetchError(f"Response from {uri} is {declared}, not an ActivityPub media type")

    def _parse(self, uri: str, response: HttpResponse) -> JSONObject:
        self._check_media_type(uri, response)
        body = response.body or b""
        if len(body) > self._policy.max_bytes:
            raise FetchError(
                f"Response from {uri} is {len(body)} bytes, limit is {self._policy.max_bytes}"
            )
        try:
            document = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise FetchError(f"Invalid JSON from {uri}: {exc}") from exc
        if not isinstance(document, dict):
            raise FetchError(f"Expected a JSON object from {uri}")
        document_id = document.get("id")
        if not document_id:
            raise FetchError(f"Document from {uri} does not name itself")
        if cache_key(str(document_id)) != uri:
            raise FetchError(f"Document id {document_id} does not match {uri}")
        return cast(JSONObject, document)
