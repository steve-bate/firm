from typing import cast

from firm.core.interfaces import (
    FIRM_NS,
    JSONObject,
    ResourceStore,
    Tenant,
    get_uri_prefix,
)
from firm.core.services.exception import MissingStore, ServiceException
from firm.core.util import get_version


async def nodeinfo_index(request_url: str) -> JSONObject:
    return {
        "links": [
            {
                "rel": "http://nodeinfo.diaspora.software/ns/schema/2.0",
                "href": f"{get_uri_prefix(request_url)}/nodeinfo/2.0",
            }
        ]
    }


class UnsupportedNodeInfoVersion(ServiceException):
    def __init__(self):
        super().__init__("Unsupported NodeInfo version")


async def nodeinfo_version(tenant: Tenant, version: str) -> JSONObject:
    if version != "2.0":
        raise UnsupportedNodeInfoVersion()

    store: ResourceStore | None = tenant.public_store
    if not store:
        raise MissingStore()

    custom_metadata = await tenant.private_store.query_one(
        {
            "type": FIRM_NS.NodeInfo.value,
            "attributedTo": tenant.prefix,
        }
    )

    core_version = get_version("firm")

    if custom_metadata and "metadata" in custom_metadata:
        metadata = cast(dict, custom_metadata["metadata"])
    else:
        metadata = {
            "other_protocols": ["webfinger", "nodeinfo"],
        }

    nodeinfo_data: JSONObject = {
        "version": "2.0",
        "software": {"name": "firm", "version": core_version},
        "protocols": ["activitypub"],
        "services": {"outbound": [], "inbound": []},
        "usage": {
            "users": {},
        },
        "openRegistrations": False,
        "metadata": cast(JSONObject, metadata),
    }

    return nodeinfo_data
