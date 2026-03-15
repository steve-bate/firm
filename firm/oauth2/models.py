from typing import List

from pydantic import BaseModel


class OAuth2Client(BaseModel):
    client_id: str
    client_secret: str | None
    redirect_uris: List[str]
    grant_types: List[str]
    response_types: List[str]
    scope: str = ""
    token_endpoint_auth_method: str = "client_secret_basic"


class OAuth2Token(BaseModel):
    access_token: str
    client_id: str
    user_id: str
    scope: str
    issued_at: int
    expires_in: int
    token_type: str = "bearer"


class AuthorizationCode(BaseModel):
    code: str
    client_id: str
    redirect_uri: str
    scope: str
    user_id: str
    issued_at: int
    expires_in: int = 600  # 10 minutes


class RefreshToken(BaseModel):
    refresh_token: str
    client_id: str
    user_id: str
    scope: str
    issued_at: int
    expires_in: int = 2_592_000  # 30 days
