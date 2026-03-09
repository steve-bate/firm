from typing import cast

from cachetools.keys import hashkey
from cachetools_async import cached  # from cachetools-async

from firm.core.interfaces import (
    FIRM_NS,
    APActor,
    HttpRequest,
    JSONObject,
    Principal,
    Tenant,
)


@cached(cache={}, key=lambda _, user_id: hashkey(user_id))
async def _get_actor(tenant: Tenant, user_id: str) -> JSONObject | None:
    return await tenant.public_store.query_one(
        {
            "preferredUsername": user_id,
        }
    )


class OAuth2BearerTokenAuthenticator:
    async def authenticate(self, request: HttpRequest) -> Principal | None:
        if "Authorization" not in request.headers:
            return None
        auth = request.headers["Authorization"]
        if not isinstance(auth, str):
            return None
        scheme, credentials = auth.split()
        if scheme.lower() != "bearer":
            return None
        tenant = request.state.tenant
        if credential_resource := await tenant.private_store.query_one(
            {
                "type": FIRM_NS.OAuth2Token.value,
                "access_token": credentials,
            }
        ):
            actor = await _get_actor(tenant, credential_resource["user_id"])
            return Principal(cast(APActor, actor), request.state.tenant) if actor else None
        else:
            return None
