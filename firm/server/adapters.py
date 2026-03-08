from typing import Any, AsyncIterable, Generator, Iterable, MutableMapping, cast

import httpx
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    BaseUser,
    UnauthenticatedUser,
)
from starlette.requests import HTTPConnection, Request
from starlette.responses import Response

from firm.core.interfaces import (
    DEFAULT_HTTP_TIMEOUT,
    Authenticator,
    HttpApplication,
    HttpMethod,
    HttpRequest,
    HttpRequestSigner,
    HttpRequestState,
    HttpResponse,
    HttpTransport,
    Identity,
    JSONObject,
    Mapping,
    Url,
    UrlTypes,
)


class HttpxAuthAdapter(httpx.Auth):
    def __init__(self, auth: HttpRequestSigner) -> None:
        super().__init__()
        self._auth = auth

    def auth_flow(self, request: Request) -> Generator[Request, Response, None]:
        self._auth.sign(HttpxRequestAdapter(request))
        return super().auth_flow(request)


class HttpxRequestAdapter(HttpRequest):
    def __init__(self, request: httpx.Request):
        self._request = request

    @property
    def method(self) -> HttpMethod:
        """The HTTP method of the request (e.g., 'GET', 'POST')."""
        return self._request.method

    @property
    def url(self) -> Url:
        """The full URL of the request."""
        # should have an adapter for URL?
        return self._request.url

    @property
    def path_params(self) -> Mapping[str, str]:
        """The path parameters of the request."""
        return {}

    @property
    def headers(self) -> MutableMapping[str, str]:
        """The request headers."""
        return self._request.headers

    @property
    def cookies(self) -> MutableMapping[str, str]:
        """The cookies sent with the request."""
        return {}

    def stream(self) -> AsyncIterable[bytes]:
        """Asynchronous stream of the request body."""
        raise NotImplementedError()

    async def body(self) -> bytes:
        """Read the entire request body at once as bytes."""
        return self._request.content

    def content(self) -> bytes:
        return self._request.content

    async def json(self) -> Mapping[str, Any]:
        """Parse the request body as JSON."""
        raise NotImplementedError()

    async def form(self) -> Mapping[str, str]:
        """Parse the request body as form data."""
        raise NotImplementedError()

    async def files(self) -> Mapping[str, Any]:
        """Parse the request body for file uploads."""
        raise NotImplementedError()

    @property
    def auth(self) -> Identity | None:
        """The authentication credentials provided with the request."""
        raise NotImplementedError()

    @property
    def state(self) -> HttpRequestState:
        return self.state

    @property
    def app(self) -> HttpApplication:
        """The application (for getting state)"""
        return self.app

    @property
    def base_url(self) -> str:
        """The base URL of the request (scheme + host)."""
        return self._request.url.origin

    @property
    def query_params(self) -> dict[str, list[str]]:
        """The query parameters of the request."""
        return self._request.url.params.multi_items()


class User(BaseUser):
    def __init__(self, identity: Identity):
        self._identity = identity

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return cast(str, self._identity.actor.get("preferredUsername", "Anonymous"))

    @property
    def identity(self) -> str:
        return self._identity.uri

    # awkward naming given starlette property name
    @property
    def firm_identity(self) -> Identity:
        return self._identity


class HttpConnectionAdapter(HttpRequest):
    def __init__(
        self,
        conn: HTTPConnection,
    ):
        self._conn = conn

    @property
    def method(self) -> HttpMethod:
        """The HTTP method of the request (e.g., 'GET', 'POST')."""
        return self._conn["method"]

    @property
    def url(self) -> Url:
        """The full URL of the request."""
        return self._conn.url

    @property
    def path_params(self) -> Mapping[str, str]:
        """The path parameters of the request."""
        return self._conn.path_params

    @property
    def headers(self) -> MutableMapping[str, str]:
        """The request headers."""
        return self._conn.headers

    @property
    def cookies(self) -> MutableMapping[str, str]:
        """The cookies sent with the request."""
        return self._conn.cookies

    def stream(self) -> AsyncIterable[bytes]:
        """Asynchronous stream of the request body."""
        raise NotImplementedError()

    def content(self) -> bytes:
        return self._conn.content

    async def body(self) -> bytes:
        """Read the entire request body at once as bytes."""
        return self._conn.content

    async def json(self) -> Mapping[str, Any]:
        """Parse the request body as JSON."""
        return await self._conn.json()

    async def form(self) -> Mapping[str, str]:
        """Parse the request body as form data."""
        return await self._conn.form()

    async def files(self) -> Mapping[str, Any]:
        """Parse the request body for file uploads."""
        raise NotImplementedError()

    @property
    def auth(self) -> Identity | None:
        """The authentication credentials provided with the request."""
        if isinstance(self._conn, Request) and self._conn.user and self._conn.user.is_authenticated:
            return self._conn.user.firm_identity
        return None

    @property
    def state(self) -> HttpRequestState:
        return self._conn.state

    @property
    def app(self) -> HttpApplication:
        """The application (for getting state)"""
        return self._conn.app

    @property
    def base_url(self):
        return self._conn.url.origin

    @property
    def query_params(self):
        return self._conn.url.params.multi_items()

    @property
    def connection(self):
        return self._conn


class AuthenticationBackendAdapter(AuthenticationBackend):
    def __init__(self, authenticator: Authenticator) -> None:
        super().__init__()
        self._authenticator = authenticator

    async def authenticate(self, request: HTTPConnection) -> tuple[AuthCredentials, BaseUser]:
        identity = await self._authenticator.authenticate(
            HttpConnectionAdapter(request)
        )  # Call the authenticate method
        if identity:
            return AuthCredentials(["authenticated"]), User(identity)
        return AuthCredentials(["unauthenticated"]), UnauthenticatedUser()


class HttpxTransport(HttpTransport):
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
    ) -> HttpResponse:
        async with httpx.AsyncClient(verify=verify) as client:
            response = await client.get(
                url,
                params=params,
                headers=headers,
                cookies=cookies,
                auth=HttpxAuthAdapter(auth) if auth else None,
                follow_redirects=follow_redirects,
                timeout=timeout,
            )
            return HttpResponse(
                status_code=response.status_code,
                headers=response.headers,
                body=response.content,
                reason_phrase=response.reason_phrase,
            )

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
    ) -> HttpResponse:
        async with httpx.AsyncClient(verify=verify) as client:
            response = await client.post(
                url,
                json=data,
                content=content,
                headers=headers,
                cookies=cookies,
                auth=HttpxAuthAdapter(auth) if auth else httpx.USE_CLIENT_DEFAULT,
                follow_redirects=follow_redirects,
                timeout=timeout,
            )
            return HttpResponse(
                status_code=response.status_code,
                headers=response.headers,
                body=response.content,
                reason_phrase=response.reason_phrase,
            )
