from collections.abc import Sequence

from firm.core.interfaces import Authenticator, HttpRequest, Identity


class AuthenticatorChain:
    def __init__(self, authenticators: Sequence[Authenticator]):
        self.authenticators = authenticators

    async def authenticate(self, request: HttpRequest) -> Identity | None:
        for auth in self.authenticators:
            identity = await auth.authenticate(request)
            if identity:
                return identity
        return None
