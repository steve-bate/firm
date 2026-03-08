import json
import logging
from http import HTTPStatus

import httpx

from firm.core.auth.http_signature import HttpSignatureAuth
from firm.core.interfaces import FIRM_NS, HttpException, HttpRequest, HttpResponse
from firm.server.adapters import HttpxAuthAdapter

log = logging.getLogger(__name__)


async def proxy(request: HttpRequest) -> HttpResponse:
    """Proxy request to remote instance (HTTP Signatures)"""
    if request.auth is None:
        raise HttpException(HTTPStatus.FORBIDDEN, "Authentication required")
    if log.isEnabledFor(logging.DEBUG):
        log.debug(f"Proxying request: {request.method} {request.url}")
    # get id from request form body
    # Process form data
    form_data = await request.form()
    requested_uri = form_data.get("id")
    if not requested_uri:
        raise HttpException(HTTPStatus.BAD_REQUEST, "Missing id")

    actor = request.auth.actor
    tenant = request.state.tenant
    config = request.app.state.config

    if config.is_local(requested_uri):
        # If the URI is local, fetch it from the tenant's public store
        if resource := await tenant.public_store.get(requested_uri):
            return HttpResponse(
                status_code=HTTPStatus.OK.value,
                headers={"Content-Type": "application/activity+json"},
                body=json.dumps(resource).encode("utf-8"),
                reason_phrase="OK",
            )
        else:
            raise HttpException(HTTPStatus.NOT_FOUND, "Resource not found in local storage")

    # Else do remote query
    # TODO cache the response

    credentials = await tenant.private_store.query_one(
        {
            "type": FIRM_NS.Credentials.value,
            "attributedTo": actor["id"],
        }
    )

    if not credentials or FIRM_NS.privateKey not in credentials:
        raise HttpException(HTTPStatus.UNAUTHORIZED, "No private key found for actor")

    # TODO Make the AP proxy more generic and move to firm core project

    async with httpx.AsyncClient(
        auth=HttpxAuthAdapter(HttpSignatureAuth(actor["id"], credentials[FIRM_NS.privateKey])),
        headers={"Accept": "application/activity+json"},
    ) as client:
        try:
            response = await client.get(requested_uri)
            proxy_response = HttpResponse(
                status_code=response.status_code,
                headers=response.headers,
                body=response.content,
                reason_phrase=response.reason_phrase,
            )
            return proxy_response
        except httpx.HTTPStatusError as e:
            raise HttpException(e.response.status_code, str(e))
        except httpx.RequestError as e:
            raise HttpException(HTTPStatus.BAD_GATEWAY, str(e))
