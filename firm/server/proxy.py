import json
import logging
from http import HTTPStatus
from typing import cast

import httpx
from fastapi import HTTPException, Request, Response

from firm.core.auth.http_signature import HttpSignatureAuth
from firm.core.interfaces import FIRM_NS, Principal
from firm.server.adapters import HttpxAuthAdapter

log = logging.getLogger(__name__)

_HOP_BY_HOP_HEADERS = frozenset(
    [
        "connection",
        "content-encoding",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    ]
)


async def proxy(request: Request, principal: Principal) -> Response:
    """Proxy request to remote instance (HTTP Signatures)"""
    if log.isEnabledFor(logging.DEBUG):
        log.debug(f"Proxying request: {request.method} {request.url}")
    # get id from request form body
    form_data = await request.form()
    requested_uri = form_data.get("id")
    if not requested_uri:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Missing id")

    actor = principal.actor
    tenant = request.state.tenant
    config = request.app.state.config

    if config.is_local(requested_uri):
        # If the URI is local, fetch it from the tenant's public store
        if resource := await tenant.public_store.get(requested_uri):
            return Response(
                status_code=HTTPStatus.OK.value,
                headers={"Content-Type": "application/activity+json"},
                body=json.dumps(resource).encode("utf-8"),
                reason_phrase="OK",
            )
        else:
            raise HTTPException(HTTPStatus.NOT_FOUND, "Resource not found in local storage")

    # Else do remote query
    # TODO cache the response

    credentials = await tenant.private_store.query_one(
        {
            "type": FIRM_NS.Credentials.value,
            "attributedTo": actor["id"],
        }
    )

    if not credentials or FIRM_NS.privateKey not in credentials:
        raise HTTPException(HTTPStatus.UNAUTHORIZED, "No private key found for actor")

    # TODO Make the AP proxy more generic and move to firm core project

    async with httpx.AsyncClient(
        auth=HttpxAuthAdapter(
            HttpSignatureAuth(actor["id"], cast(str, credentials[FIRM_NS.privateKey]))
        ),
        headers={"Accept": "application/activity+json, application/json;q=0.9, */*;q=0.8"},
    ) as client:
        try:
            response = await client.get(requested_uri)
            proxy_response = Response(
                status_code=response.status_code,
                headers={
                    k: v
                    for k, v in response.headers.items()
                    if k.lower() not in _HOP_BY_HOP_HEADERS
                },
                content=response.content,
            )
            return proxy_response
        except httpx.HTTPStatusError as e:
            raise HTTPException(e.response.status_code, str(e))
        except httpx.RequestError as e:
            raise HTTPException(HTTPStatus.BAD_GATEWAY, str(e))
