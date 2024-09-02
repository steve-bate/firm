from typing import cast

from firm.interfaces import FIRM_NS, APActor, HttpRequest, Principal, ResourceStore


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
        store: ResourceStore | None = request.app.state.store
        if store is None:
            return None
        if credential_resource := await store.query_one(
            {
                "@prefix": "urn:",
                "type": FIRM_NS.Credentials.value,
                FIRM_NS.token.value: credentials,
            }
        ):
            actor = await store.get(str(credential_resource["attributedTo"]))
            return Principal(cast(APActor, actor)) if actor else None
        else:
            return None
