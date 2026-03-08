from typing import Any, Sequence

from firm.core.interfaces import (
    Authenticator,
    AuthorizationDecision,
    AuthorizationService,
    HttpRequest,
    Identity,
    Tenant,
)


class AuthenticatorChain:
    def __init__(self, authenticators: Sequence[Authenticator]):
        self.authenticators = authenticators

    async def authenticate(self, request: HttpRequest) -> Identity | None:
        for auth in self.authenticators:
            identity = await auth.authenticate(request)
            if identity:
                return identity
        return None


class AuthorizationServiceChain:
    def __init__(self, authorizers: Sequence[AuthorizationService]):
        self.authorizers = authorizers

    async def is_get_authorized(
        self, tenant: Tenant, principal: Identity, obj: dict[str, Any]
    ) -> AuthorizationDecision:
        for authz in self.authorizers:
            auth_decision = await authz.is_get_authorized(tenant, principal, obj)
            if auth_decision.authorized:
                return auth_decision
        return AuthorizationDecision(False, "not authorized")

    async def is_post_authorized(
        self, tenant: Tenant, principal: Identity, box_type: str, box_uri: str
    ) -> AuthorizationDecision:
        for authz in self.authorizers:
            auth_decision = await authz.is_post_authorized(tenant, principal, box_type, box_uri)
            if auth_decision.authorized:
                return auth_decision
        return AuthorizationDecision(False, "not authorized")

    async def is_activity_authorized(
        self, tenant: Tenant, principal: Identity, activity: dict[str, Any]
    ) -> AuthorizationDecision:
        for authz in self.authorizers:
            auth_decision = await authz.is_activity_authorized(tenant, principal, activity)
            if auth_decision.authorized:
                return auth_decision
        return AuthorizationDecision(False, "not authorized")
