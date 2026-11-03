from fastapi import Depends, HTTPException, Request
from starlette.status import HTTP_403_FORBIDDEN

from firm.core.auth.bearer_token import BearerTokenAuthenticator
from firm.core.auth.chained import AuthenticatorChain
from firm.core.auth.http_basic import BasicHttpAuthenticator
from firm.core.auth.http_signature import HttpSigAuthenticator
from firm.core.interfaces import FIRM_NS, Identity, Principal
from firm.oauth2.middleware import OAuth2BearerTokenAuthenticator
from firm.server.adapters import HttpConnectionAdapter


async def get_principal(request: Request) -> Identity | None:
    """Authenticate one request, with remote keys resolvable through the app's resolver."""
    chain = AuthenticatorChain(
        [
            OAuth2BearerTokenAuthenticator(),
            BearerTokenAuthenticator(),
            HttpSigAuthenticator(resolver=request.app.state.resolver),
            BasicHttpAuthenticator(),
        ]
    )
    return await chain.authenticate(HttpConnectionAdapter(request))


async def require_admin(
    request: Request, principal: Identity | None = Depends(get_principal)
) -> Principal:
    """The principal, if its credentials on this tenant carry the ``admin`` role."""
    if not isinstance(principal, Principal):
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="admin role required")
    tenant = request.state.tenant
    credentials = await tenant.private_store.query_one(
        {"type": FIRM_NS.Credentials.value, "attributedTo": principal.actor.get("id")}
    )
    roles = credentials.get(FIRM_NS.role.value, []) if credentials else []
    if "admin" not in roles:
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="admin role required")
    return principal
