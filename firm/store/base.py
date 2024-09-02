import json
import os
from abc import ABC, abstractmethod
from typing import cast

from firm.interfaces import JSONObject, QueryCriteria


class ResourceStoreBase(ABC):
    """Base class for resource stores. Should not
    be used for type checking by store users."""

    @abstractmethod
    async def get(self, uri: str) -> JSONObject | None:
        ...

    @abstractmethod
    async def is_stored(self, uri: str) -> bool:
        ...

    @abstractmethod
    async def put(self, resource: JSONObject):
        ...

    @abstractmethod
    async def remove(self, uri: str):
        ...

    @abstractmethod
    async def query(self, criteria: QueryCriteria) -> list[JSONObject]:
        ...

    async def query_one(self, criteria: QueryCriteria) -> JSONObject | None:
        matches = await self.query(criteria)
        if len(matches) == 0:
            return None
        elif len(matches) == 1:
            return matches[0]
        else:
            raise ValueError(f"Multiple matches for query_one: {criteria}")

    async def update(self, uri: str, updates: JSONObject):
        if resource := await self.get(uri):
            # Can't change the resource identifier
            if "id" in updates:
                del updates["id"]
            resource.update(updates)
            await self.put(resource)
        else:
            raise ValueError(f"Unknown resource: {uri}")

    async def upsert(self, criteria: QueryCriteria, updates: JSONObject):
        if "id" not in criteria:
            raise ValueError(f"id must be in criteria for upsert: {criteria}")
        resource = await self.query_one(criteria)
        if resource is None:
            resource = cast(JSONObject, dict(criteria))
        # Can't change the resource identifier
        if "id" in updates:
            del updates["id"]
        resource.update(updates)
        await self.put(resource)

    @staticmethod
    def is_match(obj: JSONObject, criteria: QueryCriteria) -> bool:
        for ck, cv in criteria.items():
            if ck.startswith("@"):
                continue
            v = obj.get(ck)
            if cv not in v if isinstance(v, list) else v != cv:
                return False
        return True

    @staticmethod
    def is_json_file(path: str):
        ext = os.path.splitext(path)
        return len(ext) > 1 and ext[1] in [".json", ".jsonld"]

    async def load_resources(self, path: str) -> bool:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Path not found. path='{path}'")
        if os.path.isfile(path) and self.is_json_file(path):
            with open(path) as fp:
                data = json.load(fp)
                if isinstance(data, list):
                    for resource in data:
                        await self.put(resource)
                else:
                    await self.put(data)
                return True
        elif os.path.isdir(path):
            for base, _, files in os.walk(path):
                for file in files:
                    if self.is_json_file(file):
                        await self.load_resources(os.path.join(base, file))
            return True
        return False
