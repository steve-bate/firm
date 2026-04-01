import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Mapping, overload

from firm.core.interfaces import APActor, Identity, JSONObject, Tenant
from firm.core.services.activitypub.exceptions import NotFoundException
from firm.core.util import get_id, get_types


@dataclass(frozen=True)
class FirmContext:
    send: Callable[[JSONObject], Awaitable[None]]
    tenants: Mapping[str, Tenant]
    tenant: Tenant
    principal: Identity | None

    @overload
    async def dereference(self, resource_id: Any, *, required: Literal[True]) -> JSONObject: ...

    @overload
    async def dereference(
        self, resource_id: Any, *, required: Literal[False]
    ) -> JSONObject | None: ...

    @overload
    async def dereference(self, resource_id: Any) -> JSONObject | None: ...

    async def dereference(self, resource_id: Any, *, required: bool = False) -> JSONObject | None:
        # TODO
        if isinstance(resource_id, dict):
            resource_id = get_id(resource_id)
        if not resource_id:
            if required:
                raise ValueError("Resource ID is required")
            return None
        resource = await self.tenant.public_store.get(resource_id)
        if required and resource is None:
            raise NotFoundException(f"Resource {resource_id} not found")
        return resource


@dataclass(frozen=True)
class FirmBoxContext(FirmContext):
    box_owner: APActor
    box_uri: str


def generate_id(actor: APActor | JSONObject | str, subpath: str | JSONObject) -> str:
    if isinstance(subpath, dict):
        types = get_types(subpath)
        subpath = "_".join(map(str.lower, types)) if types else "object"
    if isinstance(actor, Mapping):
        actor_uri = get_id(actor)
    elif isinstance(actor, str):
        actor_uri = actor
    else:
        raise ValueError("Invalid actor")
    return f"{actor_uri}/{subpath}/{uuid.uuid4()}"
