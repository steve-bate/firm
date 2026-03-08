import copy
import uuid

from firm.core.interfaces import JSONObject, QueryCriteria
from firm.core.store.base import ResourceStoreBase


class MemoryResourceStore(ResourceStoreBase):
    def __init__(self) -> None:
        self._objects: dict[str, JSONObject] = {}

    async def get(self, uri: str) -> JSONObject | None:
        data = self._objects.get(uri)
        return copy.deepcopy(data) if data else None

    async def is_stored(self, uri: str) -> bool:
        return uri in self._objects

    async def put(self, resource: JSONObject) -> None:
        if "id" not in resource:
            # Assign a URI if one is not provided
            resource_id = f"urn:uuid:{uuid.uuid4()}"
            resource["id"] = resource_id
        else:
            resource_id = str(resource["id"])
        self._objects[resource_id] = resource

    async def remove(self, uri: str) -> None:
        objects = self._objects
        if uri in objects:
            del objects[uri]

    async def query(self, criteria: QueryCriteria) -> list[JSONObject]:
        matches: list[JSONObject] = []
        for obj in self._objects.values():
            if self.is_match(obj, criteria):
                matches.append(obj)
        return matches

    async def close(self) -> None:
        self._objects.clear()
