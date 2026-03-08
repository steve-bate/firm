from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import (
    Any,
    AsyncIterable,
    Iterable,
    Literal,
    Mapping,
    MutableMapping,
    Protocol,
    Sequence,
    TypeAlias,
    TypedDict,
    runtime_checkable,
)
from urllib.parse import parse_qs, urlparse

from firm.server.config import ServerConfig

# https://github.com/python/typing/issues/182#issuecomment-1320974824

JSONObject: TypeAlias = MutableMapping[str, "JSON"]

JSON: TypeAlias = JSONObject | Sequence["JSON"] | str | int | float | bool | None

ImmutableJSONObject: TypeAlias = Mapping[str, "ImmutableJSON"]

ImmutableJSON: TypeAlias = (
    ImmutableJSONObject | Sequence["ImmutableJSON"] | str | int | float | bool | None
)


class FIRM_NS(StrEnum):
    PREFIX = "https://firm.core.stevebate.dev/ns#"
    Tenant = "firm:Tenant"
    NodeInfo = "firm:NodeInfo"
    WebFinger = "firm:WebFinger"
    Credentials = "firm:Credentials"
    privateKey = "firm:privateKey"
    password = "firm:password"
    token = "firm:token"
    role = "firm:role"
    Blocks = "firm:Blocks"
    blockedActor = "firm:blockedActor"
    blockedDomain = "firm:blockedDomain"
    blockedSubnet = "firm:blockedSubnet"


@dataclass
class Tenant:
    prefix: str
    public_store: ResourceStore
    private_store: ResourceStore
    files: Path
    shared_inbox_uri: str | None = None


@runtime_checkable
class Url(Protocol):
    @property
    def scheme(self) -> str: ...

    @property
    def netloc(self) -> str: ...

    @property
    def path(self) -> str: ...

    @property
    def query(self) -> str: ...

    @property
    def hostname(self) -> str: ...

    @property
    def port(self) -> int: ...


def get_query_params(url: Url) -> dict[str, list[str]]:
    return parse_qs(url.query)


def get_uri_prefix(uri: str | Url) -> str:
    if isinstance(uri, str):
        parts = urlparse(uri)
        return f"{parts.scheme}://{parts.netloc}"
    return f"{uri.scheme}://{uri.netloc}"


URI: TypeAlias = str

QueryCriteria: TypeAlias = JSONObject


class ResourceStore(Protocol):
    async def get(self, uri: str) -> JSONObject | None: ...

    async def is_stored(self, uri: str) -> bool: ...

    async def put(self, resource: JSONObject) -> None: ...

    async def remove(self, uri: str) -> None: ...

    async def query(self, criteria: QueryCriteria) -> list[JSONObject]: ...

    async def query_one(self, criteria: QueryCriteria) -> JSONObject | None: ...

    async def update(self, uri: str, updates: JSONObject) -> None: ...

    async def upsert(self, criteria: QueryCriteria, updates: JSONObject) -> None: ...

    async def close(self) -> None:
        """Close any resources used by the store (e.g., database connections)"""
        ...


ActorTypes: TypeAlias = Literal["Person", "Service", "Group", "Application", "Organization"]

# TODO Revisit whether this is useful or not


class APActor(TypedDict):
    id: URI
    type: ActorTypes
    inbox: URI
    outbox: URI
    followers: URI
    following: URI
    likes: URI
    # liked: URI


class Identity(Protocol):
    @property
    def uri(self) -> str: ...

    @property
    def actor(self) -> APActor: ...

    @property
    def tenant(self) -> Tenant: ...


class Principal:
    def __init__(self, actor: APActor, tenant: Tenant):
        self.actor = actor
        self.tenant = tenant

    @property
    def uri(self) -> str:
        return self.actor["id"]


class HttpException(Exception):
    def __init__(
        self,
        status_code: int | HTTPStatus,
        detail: str | None = "",
        headers: dict[str, str] = {},
    ):
        super().__init__(status_code.phrase if isinstance(status_code, HTTPStatus) else str(detail))
        self.status_code = status_code.value if isinstance(status_code, HTTPStatus) else status_code
        self.headers = headers
        self.detail = detail


HttpMethod: TypeAlias = Literal["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]


class HttpApplicationState(Protocol):
    @property
    def store(self) -> ResourceStore: ...

    @property
    def authorizer(self) -> AuthorizationService | None: ...

    @property
    def tenants(self) -> Mapping[str, Tenant]:
        """Mapping of tenant URIs to Tenant objects."""
        ...

    @property
    def config(self) -> ServerConfig:
        """The server configuration."""
        ...


class HttpApplication(Protocol):
    @property
    def state(self) -> HttpApplicationState: ...


class HttpRequest(Protocol):
    @property
    def method(self) -> HttpMethod:
        """The HTTP method of the request (e.g., 'GET', 'POST')."""
        ...

    @property
    def url(self) -> Url:
        """The full URL of the request."""
        ...

    @property
    def path_params(self) -> Mapping[str, str]:
        """The path parameters of the request."""
        ...

    @property
    def headers(self) -> MutableMapping[str, str]:
        """The request headers."""
        ...

    @property
    def cookies(self) -> MutableMapping[str, str]:
        """The cookies sent with the request."""
        ...

    @property
    def base_url(self) -> str:
        """The base URL of the request (scheme + host)."""
        ...

    def content(self) -> bytes | None:
        """The request body as bytes. (synchronous)"""
        ...

    def stream(self) -> AsyncIterable[bytes]:
        """Asynchronous stream of the request body."""
        ...

    async def body(self) -> bytes:
        """Read the entire request body at once as bytes."""
        ...

    async def json(self) -> Mapping[str, Any]:
        """Parse the request body as JSON."""
        ...

    async def form(self) -> Mapping[str, str]:
        """Parse the request body as form data."""
        ...

    async def files(self) -> Mapping[str, Any]:
        """Parse the request body for file uploads."""
        ...

    @property
    def auth(self) -> Identity | None:
        """The authentication credentials provided with the request."""
        ...

    @property
    def state(self) -> HttpRequestState:
        """The request state (tenant, etc.)"""
        ...

    # TODO Remove the app level of the request state
    @property
    def app(self) -> HttpApplication:
        """The application (for getting state)"""
        ...

    @property
    def query_params(self) -> dict[str, list[str]]:
        """The query parameters of the request."""
        # return get_query_params(self.url)
        ...


class HttpRequestState(Protocol):
    @property
    def tenant(self) -> Tenant:
        """The tenant associated with the request."""
        ...

    @property
    def store(self) -> ResourceStore:
        """The resource store for the tenant."""
        ...

    @property
    def authorizer(self) -> AuthorizationService | None:
        """The authorization service for the tenant."""
        ...


class HttpResponse:
    def __init__(
        self,
        status_code: int,
        headers: dict[str, str] = {},
        body: bytes | None = None,
        data: JSONObject | None = None,
        reason_phrase: str | None = None,
        media_type: str | None = None,
    ):
        self._status_code = status_code
        self._headers = headers
        self._body = body or (json.dumps(data).encode() if data else None)
        self._data = data
        self._reason_phrase = reason_phrase
        if media_type:
            self._headers["Content-Type"] = media_type

    @property
    def status_code(self) -> int:
        """HTTP status code of the response."""
        return self._status_code

    @property
    def reason_phrase(self) -> str | None:
        """Optional reason phrase for the HTTP response."""
        return self._reason_phrase

    @property
    def media_type(self) -> str | None:
        """Optional media type of the response."""
        return self._headers.get("Content-Type")

    @property
    def headers(self) -> dict[str, str]:
        """Headers to be sent with the response."""
        return self._headers

    @property
    def body(self) -> bytes | None:
        """Body of the response as bytes."""
        return self._body

    @property
    def json(self) -> JSONObject:
        """Body of the response as JSON."""
        if self._data:
            return self._data
        elif self._body:
            return json.loads(self._body)
        raise ValueError("No JSON response")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise HttpException(self.status_code, self.reason_phrase, self.headers)


class PlainTextResponse(HttpResponse):
    def __init__(
        self,
        content: str,
        status_code: int = 200,
        headers: dict[str, str] = {},
        reason_phrase: str | None = None,
    ):
        super().__init__(
            status_code,
            headers=headers,
            media_type="text/plain",
            body=content.encode(),
            reason_phrase=reason_phrase,
        )
        self._content = content

    @property
    def content(self) -> str:
        return self._content


class JsonResponse(HttpResponse):
    def __init__(
        self,
        data: JSONObject,
        status_code: int = 200,
        headers: dict[str, str] = {},
        reason_phrase: str | None = None,
    ):
        super().__init__(
            status_code,
            headers=headers,
            media_type=headers.get("Content-Type") or "application/json",
            reason_phrase=reason_phrase,
        )
        self._data = data

    @property
    def body(self) -> bytes:
        return json.dumps(self._data).encode()


class AuthenticationError(Exception):
    def __init__(self, message: str):
        super().__init__(message)


@dataclass(frozen=True)
class AuthorizationDecision:
    authorized: bool
    reason: str | None
    status_code: int = HTTPStatus.FORBIDDEN.value


class Authenticator(Protocol):
    async def authenticate(self, request: HttpRequest) -> Identity | None: ...


class AuthorizationService(Protocol):
    async def is_get_authorized(
        self,
        tenant: Tenant,
        principal: Identity | None,
        resource: JSONObject,
    ) -> AuthorizationDecision:
        """Decide if an object retrieval is authorized."""
        ...

    async def is_post_authorized(
        self,
        tenant: Tenant,
        principal: Identity | None,
        box_type: str,
        box_uri: str,
    ) -> AuthorizationDecision:
        """Decide if POST is authorized"""
        ...

    async def is_activity_authorized(
        self,
        tenant: Tenant,
        principal: Identity | None,
        activity: JSONObject,
    ) -> AuthorizationDecision:
        """Decide if an activity is authorized."""
        ...


class DeliveryService(Protocol):
    async def deliver(
        self,
        tenant: Tenant,
        all_tenants: Mapping[str, Tenant],
        activity: JSONObject,
    ) -> None: ...


UrlTypes: TypeAlias = str | Url

DEFAULT_HTTP_TIMEOUT = 5.0


class HttpRequestSigner(Protocol):
    def sign(self, request: HttpRequest) -> None: ...


class HttpTransport(Protocol):
    async def get(
        self,
        url: UrlTypes,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        auth: HttpRequestSigner | None = None,
        # proxy: ProxyTypes | None = None,
        # proxies: ProxiesTypes | None = None,
        follow_redirects: bool = False,
        # cert: CertTypes | None = None,
        verify: bool = True,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
        # trust_env: bool = True,
    ) -> HttpResponse: ...

    async def post(
        self,
        url: UrlTypes,
        content: str | bytes | Iterable[bytes] | AsyncIterable[bytes] | None = None,
        data: Mapping[str, Any] | None = None,
        # files: RequestFiles | None = None,
        json: JSONObject | None = None,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        auth: HttpRequestSigner | None = None,
        # proxy: ProxyTypes | None = None,
        # proxies: ProxiesTypes | None = None,
        follow_redirects: bool = False,
        # cert: CertTypes | None = None,
        verify: bool = True,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
        # trust_env: bool = True,
    ) -> HttpResponse: ...


class Validator(Protocol):
    def validate(self, data: JSONObject) -> None:
        """Throws an exception if the object is invalid"""
        ...

    # TODO Add Link validation, if needed


class NoOpValidator(Validator):
    def validate(self, data: JSONObject) -> None:
        pass
