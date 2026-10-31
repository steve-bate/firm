from __future__ import annotations

import json
from dataclasses import dataclass, field
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


class FIRM_NS(StrEnum):
    PREFIX = "https://firm.core.stevebate.dev/ns#"
    Tenant = "firm:Tenant"
    NodeInfo = "firm:NodeInfo"
    WebFinger = "firm:WebFinger"
    Credentials = "firm:Credentials"
    OAuth2Token = "firm:OAuth2Token"
    Blocks = "firm:Blocks"
    privateKey = "firm:privateKey"
    password = "firm:password"
    token = "firm:token"
    role = "firm:role"
    blockedActor = "firm:blockedActor"
    blockedDomain = "firm:blockedDomain"
    blockedSubnet = "firm:blockedSubnet"
    collections = "firm:collections"


@dataclass
class Tenant:
    prefix: str
    public_store: ResourceStore
    private_store: ResourceStore
    files: Path
    endpoints: JSONObject = field(default_factory=dict)

    @property
    def shared_inbox_uri(self) -> str | None:
        return str(self.endpoints.get("sharedInbox")) if "sharedInbox" in self.endpoints else None


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


def get_query_params(url: Url | str) -> dict[str, list[str]]:
    qs = urlparse(url).query if isinstance(url, str) else url.query
    return parse_qs(qs)


def get_uri_prefix(uri: Url | str) -> str:
    if isinstance(uri, str):
        parts = urlparse(uri)
        return f"{parts.scheme}://{parts.netloc}"
    return f"{uri.scheme}://{uri.netloc}"


URI: TypeAlias = str

QueryCriteria: TypeAlias = JSONObject

DEFAULT_COLLECTION_PAGE_SIZE = 20
MAX_COLLECTION_PAGE_SIZE = 100

DEFAULT_SEARCH_PAGE_SIZE = 20
MAX_SEARCH_PAGE_SIZE = 100

DEFAULT_TEXT_FIELDS: tuple[str, ...] = ("summary", "content")
"""The document fields a bare search term is looked for in."""


class UnknownCollectionError(ValueError):
    """A collection that the store does not hold.

    A ValueError, so any caller catching ValueError keeps working; a distinct type, so the
    HTTP layer can map it to 404 rather than 500.
    """


START_PREFIX = "start:"
"""How a page token that means "begin at this newest-first index" is written.

A walk backwards cannot be expressed as "resume after an item" — every backend's cursor
means the next item *older* than the one it names — so the token for the previous page names
the index to begin at instead. ``page_items`` accepts either form.
"""


def encode_start(index: int) -> str:
    """A page token for the window that begins at ``index``, newest first."""
    return f"{START_PREFIX}{max(index, 0)}"


def decode_start(token: str) -> int | None:
    """The index a start token names, or None when the token is an ordinary cursor."""
    if not token.startswith(START_PREFIX):
        return None
    try:
        return max(int(token[len(START_PREFIX) :]), 0)
    except ValueError:
        raise ValueError(f"Malformed collection cursor: {token}") from None


@dataclass(frozen=True)
class CollectionPage:
    """One page of a collection's item list, newest first.

    ``cursor`` names the position of the last item returned and is passed back to
    ``ResourceStore.page_items`` for the next page; it is ``None`` when the walk is
    exhausted. It is opaque: only the store that produced it may interpret it.
    ``prev_cursor`` names the window before this one, or ``None`` at the start.
    ``total`` is how many items the collection holds — the wire's ``totalItems``.
    ``start_index`` is the position of the page's first item — the wire's ``startIndex``.
    """

    items: tuple[str, ...] = ()
    cursor: str | None = None
    prev_cursor: str | None = None
    total: int = 0
    start_index: int = 0


@dataclass(frozen=True)
class SearchPage:
    """One page of search results, best matches first.

    ``documents`` are the matching resources themselves, not their URIs: a search answers
    with a window of the documents it matched, and every backend matches on content, so
    fetching them again by id would be a second read of what the store already had.
    ``total`` is how many documents matched — the wire's ``totalItems``, and by AS2
    convention a best effort: it counts matches, before the caller drops the ones the
    principal may not see. ``start_index`` is the position of the first document returned.
    """

    documents: tuple[JSONObject, ...] = ()
    total: int = 0
    start_index: int = 0


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

    async def append_item(self, collection_uri: str, item_uri: str) -> int:
        """Add item_uri as the collection's newest item; return the item count.

        Items are not required to be unique: appending an item the collection already
        holds adds a second entry, because uniqueness is the caller's policy, not storage's.
        Raises ``UnknownCollectionError`` for a collection that does not exist.
        """
        ...

    async def remove_item(self, collection_uri: str, item_uri: str) -> int:
        """Remove item_uri if the collection holds it; return the item count.

        Removing an absent item writes nothing. Raises ``UnknownCollectionError`` for a
        collection that does not exist.
        """
        ...

    async def contains_item(self, collection_uri: str, item_uri: str) -> bool:
        """Whether the collection holds item_uri. An unknown collection holds none."""
        ...

    async def count_items(self, collection_uri: str) -> int:
        """How many items the collection holds. An unknown collection has none."""
        ...

    async def page_items(
        self,
        collection_uri: str,
        cursor: str | None = None,
        *,
        start: int | None = None,
        limit: int = DEFAULT_COLLECTION_PAGE_SIZE,
    ) -> CollectionPage:
        """Up to ``limit`` item URIs, newest first, starting after ``cursor``.

        ``cursor=None`` starts at the newest item. ``start`` names the newest-first index to
        begin at, which is how a page number is answered in one read; it and ``cursor`` are
        mutually exclusive. The returned page's cursor is ``None`` when the walk is
        exhausted, and its ``prev_cursor`` names the window before this one. An unknown
        collection is an empty page.
        """
        ...

    async def search(
        self,
        query: str,
        *,
        text_fields: Sequence[str] = DEFAULT_TEXT_FIELDS,
        start: int = 0,
        limit: int = DEFAULT_SEARCH_PAGE_SIZE,
    ) -> SearchPage:
        """Up to ``limit`` documents matching ``query``, starting at match ``start``.

        ``query`` is the search syntax ``firm.search.parser`` accepts — bare terms,
        quoted phrases, ``field:value`` facets and AND/OR/NOT — and each backend answers
        it as well as its layout allows; a backend that cannot push the query down still
        matches it, slowly. ``text_fields`` names the document fields an unfielded term is
        looked for in. The returned page's ``total`` counts matches, not the subset the
        caller may be allowed to see.
        """
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
        #
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


@dataclass(frozen=True)
class DeliveryFailure:
    """One inbox the activity did not reach.

    ``inbox`` is ``None`` for a failure that happened before any inbox was chosen:
    no id or actor on the activity, or no signing key for the actor.
    """

    inbox: str | None
    attempts: int
    reason: str


@dataclass(frozen=True)
class DeliveryReport:
    """What happened to one activity: the inboxes written to, the ones that failed."""

    delivered: tuple[str, ...] = ()
    failed: tuple[DeliveryFailure, ...] = ()


class DeliveryService(Protocol):
    async def deliver(
        self,
        tenant: Tenant,
        all_tenants: Mapping[str, Tenant],
        activity: JSONObject,
        *,
        local_only: bool = False,
    ) -> DeliveryReport: ...


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
