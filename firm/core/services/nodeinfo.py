from http import HTTPStatus
from typing import cast

from firm.core.interfaces import (
    FIRM_NS,
    HttpException,
    HttpRequest,
    JSONObject,
    JsonResponse,
    ResourceStore,
    get_uri_prefix,
)
from firm.core.util import get_version


async def nodeinfo_index(request: HttpRequest) -> JsonResponse:
    return JsonResponse(
        {
            "links": [
                {
                    "rel": "http://nodeinfo.diaspora.software/ns/schema/2.0",
                    "href": f"{get_uri_prefix(request.url)}/nodeinfo/2.0",
                }
            ]
        },
        headers={"Content-Type": "application/jrd+json"},
    )


async def nodeinfo_version(request: HttpRequest) -> JsonResponse:
    version = request.path_params["version"]
    if version != "2.0":
        raise HttpException(HTTPStatus.NOT_FOUND, "Only nodeinfo 2.0 supported")

    tenant = request.state.tenant
    store: ResourceStore | None = tenant.public_store
    if not store:
        raise HttpException(HTTPStatus.INTERNAL_SERVER_ERROR.value, "No store")

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
            "component_versions": {
                "core": core_version,
            },
            "other_protocols": ["webfinger", "nodeinfo"],
        }
        if server_version := get_version("firm-server"):
            metadata["component_versions"]["server"] = server_version

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

    return JsonResponse(nodeinfo_data)
