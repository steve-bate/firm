from fastapi import Request

from firm.core.auth.bearer_token import BearerTokenAuthenticator
from firm.core.auth.chained import AuthenticatorChain
from firm.core.auth.http_basic import BasicHttpAuthenticator
from firm.core.auth.http_signature import HttpSigAuthenticator
from firm.core.interfaces import Identity
from firm.oauth2.middleware import OAuth2BearerTokenAuthenticator
from firm.server.adapters import HttpConnectionAdapter

_auth_chain = AuthenticatorChain(
    [
        OAuth2BearerTokenAuthenticator(),
        BearerTokenAuthenticator(),
        HttpSigAuthenticator(),
        BasicHttpAuthenticator(),
    ]
)


async def get_principal(request: Request) -> Identity | None:
    return await _auth_chain.authenticate(HttpConnectionAdapter(request))
