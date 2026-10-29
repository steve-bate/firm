"""Cache entries and the backends that persist them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from firm.core.interfaces import JSONObject


@dataclass(frozen=True)
class CacheEntry:
    """One answer about one URI: a document, or a remembered failure."""

    uri: str
    document: JSONObject | None
    status_code: int | None
    fetched_at: float
    expires_at: float

    def is_fresh(self, now: float) -> bool:
        """Whether this entry may be served without asking the origin again."""
        return now < self.expires_at

    @property
    def is_negative(self) -> bool:
        """A remembered failure rather than a document."""
        return self.document is None


@runtime_checkable
class RemoteCache(Protocol):
    """Where entries live. Keys are cache keys, not raw URIs."""

    async def get(self, key: str) -> CacheEntry | None: ...

    async def put(self, entry: CacheEntry) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def purge_expired(self, now: float) -> int: ...

    async def close(self) -> None: ...


class MemoryRemoteCache:
    """An in-process cache, unbounded by design."""

    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}

    async def get(self, key: str) -> CacheEntry | None:
        return self._entries.get(key)

    async def put(self, entry: CacheEntry) -> None:
        self._entries[entry.uri] = entry

    async def delete(self, key: str) -> None:
        self._entries.pop(key, None)

    async def purge_expired(self, now: float) -> int:
        expired = [key for key, entry in self._entries.items() if not entry.is_fresh(now)]
        for key in expired:
            del self._entries[key]
        return len(expired)

    async def close(self) -> None:
        self._entries.clear()
