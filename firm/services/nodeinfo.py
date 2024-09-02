from http import HTTPStatus
from typing import cast

from firm.interfaces import (
    FIRM_NS,
    HttpException,
    HttpRequest,
    JSONObject,
    JsonResponse,
    ResourceStore,
    get_url_prefix,
)
from firm.util import get_version


async def nodeinfo_index(request: HttpRequest) -> JsonResponse:
    return JsonResponse(
        {
            "links": [
                {
                    "rel": "http://nodeinfo.diaspora.software/ns/schema/2.0",
                    "href": f"{get_url_prefix(request.url)}/nodeinfo/2.0",
                }
            ]
        },
        headers={"Content-Type": "application/jrd+json"},
    )


async def nodeinfo_version(request: HttpRequest) -> JsonResponse:
    version = request.path_params["version"]
    if version != "2.0":
        raise HttpException(HTTPStatus.NOT_FOUND, "Only nodeinfo 2.0 supported")

    store: ResourceStore | None = request.app.state.store
    if not store:
        raise HttpException(HTTPStatus.INTERNAL_SERVER_ERROR.value, "No store")

    prefix = get_url_prefix(request.url)

    custom_metadata = await store.query_one(
        {
            "@prefix": "urn:",  # private
            "type": FIRM_NS.NodeInfo.value,
            "attributedTo": prefix,
        }
    )
    metadata = (
        custom_metadata["metadata"]
        if custom_metadata and "metadata" in custom_metadata
        else {
            "nodeName": "FIRM",
            "nodeDescription": "A FIRM server",
        }
    )

    nodeinfo_data: JSONObject = {
        "version": "2.0",
        "software": {"name": "firm", "version": get_version("firm")},
        "protocols": ["activitypub"],
        "services": {"outbound": [], "inbound": []},
        "usage": {
            "users": {},
        },
        "openRegistrations": False,
        "metadata": cast(JSONObject, metadata),
    }

    return JsonResponse(nodeinfo_data)
