import base64
import binascii
import logging
from http import HTTPStatus
from typing import cast

import bcrypt

from firm.interfaces import (
    FIRM_NS,
    APActor,
    AuthenticationError,
    HttpException,
    HttpRequest,
    Identity,
    Principal,
)

log = logging.getLogger(__name__)


class BasicHttpAuthenticator:
    async def authenticate(self, request: HttpRequest) -> Identity | None:
        if "Authorization" not in request.headers:
            # _logger.info("Unauthenticated access")
            # return AuthCredentials(None), UnauthenticatedUser()
            return None

        auth = request.headers["Authorization"]
        if not isinstance(auth, str):
            return None

        try:
            scheme, credentials = auth.split()
            if scheme.lower() != "basic":
                return None
            decoded = base64.b64decode(credentials).decode()
        except (ValueError, UnicodeDecodeError, binascii.Error):
            raise AuthenticationError("Invalid basic auth credentials")

        idx = decoded.rindex(":")
        actor_uri = decoded[:idx]
        password = decoded[idx + 1 :]

        store = request.app.state.store
        credentials_resource = await store.query_one(
            {"type": FIRM_NS.Credentials.value, "attributedTo": actor_uri}
        )

        if (
            credentials_resource
            and FIRM_NS.password in credentials_resource
            and verify_hash(password, str(credentials_resource[FIRM_NS.password]))
        ):
            log.info("Authentication succeeded: %s", actor_uri)
            actor = await store.get(actor_uri)
            return Principal(cast(APActor, actor))
        else:
            # _logger.info("Authentication failed: %s", uri)
            # return AuthCredentials(None), UnauthenticatedUser()
            return None


def verify_hash(provided_pass: str, user_pass: str) -> bool:
    hashed_password = bcrypt.hashpw(provided_pass.encode(), user_pass.encode()).decode()
    return hashed_password == user_pass


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode()


def basic_auth_challenge():
    raise HttpException(
        status_code=HTTPStatus.UNAUTHORIZED.value,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Basic"},
    )
