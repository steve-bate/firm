from typing import Any, Sequence

from firm.interfaces import (
    Authenticator,
    AuthorizationDecision,
    AuthorizationService,
    HttpRequest,
    Identity,
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
        self, principal: Identity, obj: dict[str, Any]
    ) -> AuthorizationDecision:
        for authz in self.authorizers:
            auth_decision = await authz.is_get_authorized(principal, obj)
            if auth_decision.authorized:
                return auth_decision
        return AuthorizationDecision(False, "not authorized")

    async def is_post_authorized(
        self, principal: Identity, box_type: str, box_uri: str
    ) -> AuthorizationDecision:
        for authz in self.authorizers:
            auth_decision = await authz.is_post_authorized(principal, box_type, box_uri)
            if auth_decision.authorized:
                return auth_decision
        return AuthorizationDecision(False, "not authorized")

    async def is_activity_authorized(
        self, principal: Identity, activity: dict[str, Any]
    ) -> AuthorizationDecision:
        for authz in self.authorizers:
            auth_decision = await authz.is_activity_authorized(principal, activity)
            if auth_decision.authorized:
                return auth_decision
        return AuthorizationDecision(False, "not authorized")
