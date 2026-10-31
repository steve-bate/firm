import logging
from functools import cache
from urllib.parse import urlparse

from firm.core.interfaces import (
    DEFAULT_COLLECTION_PAGE_SIZE,
    CollectionPage,
    JSONObject,
    QueryCriteria,
    ResourceStore,
    get_uri_prefix,
)

log = logging.getLogger(__name__)


class PrefixAwareResourceStore(ResourceStore):
    def __init__(
        self,
        tenant_public_stores: dict[str, ResourceStore],
        tenant_private_stores: dict[str, ResourceStore],
        remote_store: ResourceStore,
    ):
        self.tenant_public_stores = tenant_public_stores
        self.tenant_private_stores = tenant_private_stores
        self.remote_store = remote_store

    @staticmethod
    def _is_private(prefix: str) -> bool:
        return prefix.startswith("private:")

    def _is_tenant(self, prefix: str) -> bool:
        return prefix in self.tenant_public_stores or prefix in self.tenant_private_stores

    @cache
    def _get_store_for_prefix(self, prefix: str) -> ResourceStore:
        if not self._is_tenant(prefix):
            return self.remote_store
        stores = (
            self.tenant_private_stores if self._is_private(prefix) else self.tenant_public_stores
        )
        store = stores.get(prefix) or stores.get("*")
        if store is None:
            raise ValueError(f"No store for {prefix}")
        return store

    def _get_store_for_uri(self, uri: str) -> ResourceStore:
        return self._get_store_for_prefix(get_uri_prefix(uri))

    async def get(self, uri: str) -> JSONObject | None:
        return await self._get_store_for_uri(uri).get(uri)

    async def is_stored(self, uri: str) -> bool:
        return await self._get_store_for_uri(uri).is_stored(uri)

    async def put(self, resource: JSONObject) -> None:
        uri = str(resource.get("id"))
        if uri is None:
            raise ValueError("Resource has no id")
        return await self._get_store_for_uri(uri).put(resource)

    async def remove(self, uri: str) -> None:
        return await self._get_store_for_uri(uri).remove(uri)

    def _get_prefix(self, criteria: QueryCriteria) -> str:
        prefix = criteria.pop("@prefix", None)
        if prefix is None:
            # prefix = criteria.get("attributedTo")
            # FIXME There must be a prefix
            if prefix is None:
                raise ValueError("Query criteria has no @prefix")
            # # TODO For now, just return a dummy prefix
            # prefix = "urn:unknown"
        parts = urlparse(str(prefix))
        return f"{parts.scheme}://{parts.netloc}"

    async def query(self, criteria: QueryCriteria) -> list[JSONObject]:
        return await self._get_store_for_prefix(self._get_prefix(criteria)).query(criteria)

    async def query_one(self, criteria: QueryCriteria) -> JSONObject | None:
        prefix = self._get_prefix(criteria)
        store = self._get_store_for_prefix(prefix)
        return await store.query_one(criteria)

    async def update(self, uri: str, updates: JSONObject) -> None:
        return await self._get_store_for_uri(uri).update(uri, updates)

    async def upsert(self, criteria: QueryCriteria, updates: JSONObject) -> None:
        return await self._get_store_for_prefix(self._get_prefix(criteria)).upsert(
            criteria, updates
        )

    async def append_item(self, collection_uri: str, item_uri: str) -> int:
        store = self._get_store_for_uri(collection_uri)
        return await store.append_item(collection_uri, item_uri)

    async def remove_item(self, collection_uri: str, item_uri: str) -> int:
        store = self._get_store_for_uri(collection_uri)
        return await store.remove_item(collection_uri, item_uri)

    async def contains_item(self, collection_uri: str, item_uri: str) -> bool:
        store = self._get_store_for_uri(collection_uri)
        return await store.contains_item(collection_uri, item_uri)

    async def count_items(self, collection_uri: str) -> int:
        store = self._get_store_for_uri(collection_uri)
        return await store.count_items(collection_uri)

    async def page_items(
        self,
        collection_uri: str,
        cursor: str | None = None,
        *,
        limit: int = DEFAULT_COLLECTION_PAGE_SIZE,
    ) -> CollectionPage:
        store = self._get_store_for_uri(collection_uri)
        return await store.page_items(collection_uri, cursor, limit=limit)

    async def close(self) -> None:
        for store in self.tenant_private_stores.values():
            await store.close()
        for store in self.tenant_public_stores.values():
            await store.close()
        await self.remote_store.close()
