from typing import cast

from firm.core.interfaces import FIRM_NS, APActor, HttpRequest, Principal


class BearerTokenAuthenticator:
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
                "type": FIRM_NS.Credentials.value,
                FIRM_NS.token.value: credentials,
            }
        ):
            actor = await tenant.public_store.get(str(credential_resource["attributedTo"]))
            return Principal(cast(APActor, actor), request.state.tenant) if actor else None
        else:
            return None
