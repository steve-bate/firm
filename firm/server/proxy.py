import json
import logging
from http import HTTPStatus
from typing import cast

import httpx
from fastapi import HTTPException, Request, Response

from firm.core.auth.http_signature import HttpSignatureAuth
from firm.core.interfaces import FIRM_NS, JSONObject, Principal, Tenant
from firm.core.services.activitypub.support import FirmContext
from firm.server.adapters import HttpxAuthAdapter

log = logging.getLogger(__name__)

AP_MEDIA_TYPE = "application/activity+json"


async def proxy(request: Request, principal: Principal) -> Response:
    """Dereference one document for the authenticated caller.

    The caller has already been authenticated; ``principal`` is the identity that asks,
    and the actor it names is the one that signs any outbound GET. Two kinds of URI
    arrive here:

    - **A URI this instance owns** is answered from the *owning* tenant's store. Ownership
      comes from the tenant registry: another tenant of the same instance is local, and
      its documents are in its own store, not in the requesting tenant's.
    - **Anything else** is answered from the remote cache when this server already holds
      the document, and fetched — signed as the caller's actor — otherwise. The cache is
      what makes an inbox item readable: a peer need not serve its activity URIs publicly
      (Pixelfed answers 404 to an anonymous GET of `…/p/alice/<id>/activity`), and this
      server was handed the document when it was delivered.

    This is a read, so every document leaves through the same decision the AP endpoint
    applies: ``is_get_authorized`` for the tenant that holds it and the principal asking.
    Finding the document answers "where is it", never "may I see it": a cached document
    was cached for one delivery rather than for everyone on the instance, and a tenant's
    store keeps its private-addressed documents beside its public ones.
    """
    if log.isEnabledFor(logging.DEBUG):
        log.debug(f"Proxying request: {request.method} {request.url}")
    form_data = await request.form()
    requested_uri = form_data.get("id")
    if not requested_uri:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Missing id")
    uri = str(requested_uri)

    tenant = request.state.tenant
    resolver = request.app.state.resolver
    context = FirmContext(resolver, tenant, principal, request.app.state.authorizer)

    owner = resolver.owner(uri)
    if owner is not None:
        if resource := await owner.public_store.get(uri):
            return _served(await _read(context, owner, resource))
        raise HTTPException(HTTPStatus.NOT_FOUND, "Resource not found in local storage")

    if cached := await resolver.peek(uri):
        return _served(await _read(context, tenant, cached))

    actor = principal.actor
    credentials = await tenant.private_store.query_one(
        {
            "type": FIRM_NS.Credentials.value,
            "attributedTo": actor["id"],
        }
    )

    if not credentials or FIRM_NS.privateKey not in credentials:
        raise HTTPException(HTTPStatus.UNAUTHORIZED, "No private key found for actor")

    async with httpx.AsyncClient(
        auth=HttpxAuthAdapter(
            HttpSignatureAuth(actor["id"], cast(str, credentials[FIRM_NS.privateKey]))
        ),
        headers={"Accept": "application/activity+json"},
    ) as client:
        try:
            response = await client.get(uri)
            document = _document_of(response)
            if document is not None:
                # Remembering it is what makes the next read local, and what lets a
                # client render a document the origin will not serve again.
                await resolver.remember(document)
                return _served(await _read(context, tenant, document))
            return Response(
                status_code=response.status_code,
                headers=response.headers,
                content=response.content,
            )
        except httpx.HTTPStatusError as e:
            raise HTTPException(e.response.status_code, str(e))
        except httpx.RequestError as e:
            raise HTTPException(HTTPStatus.BAD_GATEWAY, str(e))


async def _read(context: FirmContext, held_by: Tenant, document: JSONObject) -> JSONObject:
    """``document``, or a 403 naming the decision that refused the read.

    ``held_by`` is the tenant the document came from: a read of another instance's
    document gets *that* instance's answer, which is the rule ``FirmContext._resolve``
    already applies to a cross-instance read.
    """
    decision = await context.authorizer.is_get_authorized(held_by, context.principal, document)
    if not decision.authorized:
        log.info(
            "Proxy read denied by %s: %s (%s)",
            held_by.prefix,
            document.get("id"),
            decision.reason,
        )
        raise HTTPException(HTTPStatus.FORBIDDEN, decision.reason or "Not authorized")
    return document


def _document_of(response: httpx.Response) -> dict | None:
    """The JSON object a 200 response carries, or None when it is not one."""
    if response.status_code != HTTPStatus.OK:
        return None
    if "json" not in (response.headers.get("content-type") or ""):
        return None
    try:
        document = response.json()
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def _served(document: JSONObject) -> Response:
    return Response(
        status_code=HTTPStatus.OK.value,
        headers={"Content-Type": AP_MEDIA_TYPE},
        content=json.dumps(document).encode("utf-8"),
    )
