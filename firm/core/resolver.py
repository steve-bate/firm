"""Routing: which local instance owns a URI, and the fetch path for the rest."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Mapping

from firm.core.interfaces import JSONObject, Tenant
from firm.core.util import is_urn

if TYPE_CHECKING:
    from firm.core.remote.provider import RemoteResourceProvider

log = logging.getLogger(__name__)


class ResourceResolver:
    """The instance registry: which local tenant owns a URI, and the fetch path.

    It makes no access decision. A caller holding identity asks ``owner()``, reads
    that instance's store, and then asks the owner whether the read is allowed.
    """

    def __init__(
        self, tenants: Mapping[str, Tenant], remote: RemoteResourceProvider | None = None
    ) -> None:
        self._tenants = tenants
        self._remote = remote

    @property
    def tenants(self) -> Mapping[str, Tenant]:
        """Every tenant this server serves, by prefix."""
        return self._tenants

    @property
    def can_fetch(self) -> bool:
        return self._remote is not None

    def owner(self, uri: str) -> Tenant | None:
        """The local tenant whose prefix owns this URI, longest match first.

        ``None`` means no local instance owns it, so the caller may fetch it
        instead. This is a lookup in the registry, not a read of any store.
        """
        best: Tenant | None = None
        for prefix, tenant in self._tenants.items():
            if uri.startswith(prefix) and (best is None or len(prefix) > len(best.prefix)):
                best = tenant
        return best

    async def fetch(self, uri: str) -> JSONObject | None:
        """Fetch a document from another server, or ``None``.

        Only for URIs no local tenant owns: asking this for a local URI would be
        a request to ourselves by another name, so it is refused here rather than
        left to the caller to remember.
        """
        if self.owner(uri) is not None or is_urn(uri):
            log.warning("Refusing to fetch a locally owned URI: %s", uri)
            return None
        if self._remote is None:
            return None
        return await self._remote.get(uri)

    async def purge(self) -> int:
        """Drop expired cached documents. Returns the number removed."""
        if self._remote is None:
            return 0
        return await self._remote.purge()

    async def close(self) -> None:
        if self._remote is None:
            return
        await self._remote.close()
