import json
from typing import Any, AsyncIterable, Mapping, MutableMapping, cast
from urllib.parse import urlparse

from firm.interfaces import (
    APActor,
    AuthorizationDecision,
    AuthorizationService,
    DeliveryService,
    HttpApplication,
    HttpApplicationState,
    HttpMethod,
    HttpRequest,
    Identity,
    JSONObject,
    ResourceStore,
    Url,
)
from firm.store.memory import MemoryResourceStore


class StubUrl:
    def __init__(
        self,
        scheme: str,
        netloc: str,
        path: str,
        query: str,
    ):
        self._scheme = scheme
        self._netloc = netloc
        self._path = path
        self._query = query

    @staticmethod
    def parse(url: str) -> Url:
        """Parse a URL string and return a StubUrl instance."""
        parsed_url = urlparse(url)
        return StubUrl(
            scheme=parsed_url.scheme,
            netloc=parsed_url.netloc,
            path=parsed_url.path,
            query=parsed_url.query,
        )

    @property
    def scheme(self) -> str:
        """The URL scheme (e.g., 'http', 'https')."""
        return self._scheme

    @property
    def netloc(self) -> str:
        """The network location part of the URL (e.g., 'example.com:8080')."""
        return self._netloc

    @property
    def path(self) -> str:
        """The path part of the URL (e.g., '/users')."""
        return self._path

    @property
    def query(self) -> str:
        """The query string part of the URL (e.g., 'key=value&foo=bar')."""
        return self._query

    @property
    def hostname(self) -> str:
        """The hostname part of the URL (e.g., 'example.com')."""
        return self._netloc.split(":")[0]

    @property
    def port(self) -> int:
        """The port number part of the URL (e.g., 8080)."""
        return (
            int(self._netloc.split(":")[1])
            if ":" in self._netloc
            else (80 if self._scheme == "http" else 443)
        )

    def __str__(self) -> str:
        """Return a string representation of the StubUrl."""
        return f"{self._scheme}://{self._netloc}{self._path}" + (
            "?{self._query}" if self._query else ""
        )


class StubIdentity:
    def __init__(self, data: str | APActor):
        if isinstance(data, str):
            self._uri = data
            self._actor = APActor(
                id=data,
                type="Person",
                inbox="",
                outbox="",
                followers="",
                following="",
                likes="",
            )
        else:
            self._uri = data["id"]
            self._actor = cast(APActor, data)

    @property
    def uri(self) -> str:
        return self._uri

    @property
    def actor(self) -> APActor:
        return self._actor


class StubAuthorizationService(AuthorizationService):
    # implement stubs for the AuthorizationService interface
    async def is_get_authorized(
        self, principal: Identity | None, resource: JSONObject
    ) -> AuthorizationDecision:
        return AuthorizationDecision(True, None)

    async def is_post_authorized(
        self, principal: Identity | None, box_type: str, box_uri: str
    ) -> AuthorizationDecision:
        return AuthorizationDecision(True, None)

    async def is_activity_authorized(
        self,
        principal: Identity | None,
        activity: JSONObject,
    ) -> AuthorizationDecision:
        return AuthorizationDecision(True, None)


class StubDeliveryService(DeliveryService):
    async def deliver(self, activity: JSONObject) -> None:
        ...


class StubState(HttpApplicationState):
    def __init__(
        self,
        store: ResourceStore | None = None,
        authorizer: AuthorizationService | None = None,
    ) -> None:
        self._store = store or MemoryResourceStore()
        self._authorizer = authorizer

    @property
    def store(self) -> ResourceStore:
        return self._store

    @store.setter
    def store(self, store: ResourceStore) -> None:
        self._store = store

    @property
    def authorizer(self) -> AuthorizationService | None:
        return self._authorizer

    @authorizer.setter
    def authorizer(self, authorizer: AuthorizationService | None) -> None:
        self._authorizer = authorizer


class StubApplication(HttpApplication):
    def __init__(self) -> None:
        self._state = StubState()

    @property
    def state(self) -> StubState:
        return self._state

    @property
    def authorizer(self) -> AuthorizationService | None:
        return self._state.authorizer


class StubHttpRequest(HttpRequest):
    def __init__(
        self,
        method: HttpMethod,
        url: Url | str,
        headers: MutableMapping[str, str] = {},
        cookies: MutableMapping[str, str] = {},
        client: tuple[str, int] | None = None,
        body: bytes | None = None,
        form: Mapping[str, Any] | None = None,
        files: Mapping[str, Any] | None = None,
        auth: Any | None = None,
        path_params: Mapping[str, str] = {},
        store: ResourceStore | None = None,
        authorizer: AuthorizationService | None = None,
    ):
        self._method = method
        self._url = url if isinstance(url, Url) else StubUrl.parse(url)
        self._headers = headers
        self._cookies = cookies
        self._client = client
        self._body = body
        self._form = form
        self._files = files
        self._auth = auth
        self._path_params = path_params
        self._app = StubApplication()
        if store:
            self._app.state.store = store
        if authorizer:
            self._app.state.authorizer = authorizer

    @property
    def method(self) -> HttpMethod:
        """The HTTP method of the request (e.g., 'GET', 'POST')."""
        return self._method  # type: ignore

    @property
    def url(self) -> Url:
        """The full URL of the request."""
        return self._url

    @property
    def headers(self) -> MutableMapping[str, str]:
        """The request headers."""
        return self._headers

    @property
    def cookies(self) -> MutableMapping[str, str]:
        """The cookies sent with the request."""
        return self._cookies

    @property
    def client(self) -> tuple[str, int] | None:
        """The IP address and port of the client making the request."""
        return self._client

    def stream(self) -> AsyncIterable[bytes]:
        """Asynchronous stream of the request body."""
        raise NotImplementedError("TODO")

    async def body(self) -> bytes:
        """Read the entire request body at once as bytes."""
        if self._body is None:
            raise ValueError("No body to return")
        return self._body

    def content(self) -> bytes | None:
        """Return the request body if it has been read."""
        return self._body

    async def json(self) -> Mapping[str, Any]:
        """Parse the request body as JSON."""
        if self._body is None:
            raise ValueError("No body to stream")
        return json.loads(self._body.decode())

    async def form(self) -> Mapping[str, str]:
        """Parse the request body as form data."""
        # Implement form data parsing logic here
        if self._form is None:
            raise ValueError("No form data")
        return self._form

    async def files(self) -> Mapping[str, Any]:
        """Parse the request body for file uploads."""
        # Implement file upload parsing logic here
        if self._files is None:
            raise ValueError("No files uploaded")
        return self._files

    @property
    def auth(self) -> StubIdentity | None:
        """The authentication credentials provided with the request."""
        return self._auth

    @property
    def path_params(self) -> Mapping[str, str]:
        """The path parameters extracted from the request URL."""
        return self._path_params

    @property
    def app(self) -> StubApplication:
        return self._app
