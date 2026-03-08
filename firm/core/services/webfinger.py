import re

from firm.core.interfaces import (
    FIRM_NS,
    JSONObject,
    ResourceStore,
    Tenant,
)
from firm.core.services.exception import MissingStore, ServiceException
from firm.core.util import is_type

_RESOURCE_REGEX = re.compile("(?:.*?):[@~]?([^@]+)@?(.*)")
_SERVER_REL = "https://www.w3.org/ns/activitystreams#Service"


class InvalidResourceUri(ServiceException):
    def __init__(self):
        super().__init__("Invalid resource_uri format")


class ResourceNotFound(ServiceException):
    def __init__(self, resource_uri: str):
        super().__init__(f"Resource not found: {resource_uri}")


async def webfinger(
    tenant: Tenant,
    resource_uri: str,
    aka_predicates: list[str] | None = None,
) -> JSONObject:
    m = _RESOURCE_REGEX.match(resource_uri)
    if not m:
        raise InvalidResourceUri()

    store: ResourceStore | None = tenant.public_store
    if not store:
        raise MissingStore()

    resource = await store.get(resource_uri)

    if not resource:
        # TODO Make the AKA predicates configurable
        predicates = ["alsoKnownAs"] if aka_predicates is None else aka_predicates
        for p in predicates:
            resource = await store.query_one(
                {p: resource_uri},
            )
            if resource:
                break

        if not resource:
            raise ResourceNotFound(resource_uri)

    return {
        "subject": resource_uri,
        "links": [
            {
                "rel": _SERVER_REL if is_type(resource, FIRM_NS.Tenant) else "self",
                "type": "application/activity+json",
                "href": resource["id"],
                "properties": {
                    "https://www.w3.org/ns/activitystreams#type": resource["type"],
                },
            }
        ],
    }
