import uuid

from firm.interfaces import JSONObject, QueryCriteria
from firm.store.base import ResourceStoreBase


class MemoryResourceStore(ResourceStoreBase):
    def __init__(self) -> None:
        self._objects: dict[str, JSONObject] = {}

    async def get(self, uri: str) -> JSONObject | None:
        return self._objects.get(uri)

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
