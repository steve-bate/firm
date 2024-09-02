import re
from http import HTTPStatus

from firm.interfaces import (
    HttpException,
    HttpRequest,
    JsonResponse,
    ResourceStore,
    get_query_params,
    get_url_prefix,
)

_RESOURCE_REGEX = re.compile("(?:.*?):[@~]?([^@]+)@?(.*)")


async def webfinger(request: HttpRequest, aka_predicates: list[str] | None = None):
    resource_params: list[str] | None = get_query_params(request.url).get("resource")
    if resource_params is None or len(resource_params) == 0:
        raise HttpException(
            HTTPStatus.BAD_REQUEST,
            detail="Missing resource_uri param",
        )
    if len(resource_params) > 1:
        raise HttpException(
            HTTPStatus.BAD_REQUEST,
            detail="Multiple resource_uri params not supported",
        )
    resource_uri = resource_params[0]
    m = _RESOURCE_REGEX.match(resource_uri)
    if not m:
        raise HttpException(HTTPStatus.BAD_REQUEST, "Invalid resource_uri format")

    store: ResourceStore | None = request.app.state.store
    if not store:
        raise HttpException(HTTPStatus.INTERNAL_SERVER_ERROR.value, "No store")

    resource = await store.get(resource_uri)

    if not resource:
        # TODO Make the AKA predicates configurable
        predicates = ["alsoKnownAs"] if aka_predicates is None else aka_predicates
        for p in predicates:
            resource = await store.query_one(
                {"@prefix": get_url_prefix(request.url), p: resource_uri},
            )
            if resource:
                break

        if not resource:
            raise HttpException(HTTPStatus.NOT_FOUND)

    return JsonResponse(
        {
            "subject": resource_uri,
            "links": [
                {
                    "rel": "self",
                    "type": "application/activity+json",
                    "href": resource["id"],
                    "properties": {
                        "https://www.w3.org/ns/activitystreams#type": resource["type"],
                    },
                }
            ],
        },
        headers={"Content-Type": "application/jrd+json"},
    )
