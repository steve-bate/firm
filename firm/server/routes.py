import asyncio
import datetime
import logging
import time
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Awaitable, Callable

import mimeparse
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from jsonschema.exceptions import ValidationError

from firm.core.auth.authorization import CoreAuthorizationService
from firm.core.delivery.queue import Delivery, DeliveryState
from firm.core.interfaces import (
    DEFAULT_SEARCH_PAGE_SIZE,
    MAX_SEARCH_PAGE_SIZE,
    DeliveryService,
    Identity,
    JSONObject,
    Principal,
    Tenant,
    Validator,
    get_query_params,
)
from firm.core.services.activitypub.collections import option_value
from firm.core.services.activitypub.exceptions import (
    InvalidRequestException,
    InvalidResourceTypeException,
    NotAuthorizedException,
    NotFoundException,
    ResourceOwnerException,
)
from firm.core.services.activitypub.service import (
    ActivityPubService,
)
from firm.core.services.activitypub.support import FirmContext
from firm.core.services.nodeinfo import (
    UnsupportedNodeInfoVersion,
    nodeinfo_index,
    nodeinfo_version,
)
from firm.core.services.webfinger import InvalidResourceUri, ResourceNotFound, webfinger
from firm.core.util import AS2_CONTENT_TYPES
from firm.jsonschema.validation import create_validator
from firm.oauth2.router import create_oauth2_router
from firm.server.auth import get_principal, require_admin
from firm.server.config import ServerConfig
from firm.server.delivery import FirmDeliveryService
from firm.server.html.endpoint import html_endpoint, html_static_endpoint
from firm.streaming.endpoint import sse_client_page_endpoint
from firm.streaming.sse.notifier import StreamEvent, StreamNotifier, get_notifier
from firm.streaming.sse.router import create_sse_router

from .media import create_media_upload_router
from .proxy import proxy

log = logging.getLogger(__name__)


# TODO Consider redesign of FirmDeliveryService (abstract class?)
# class MimeTypeRoute(Route):
#     def __init__(self, *args, **kwargs):
#         self._mimetypes = kwargs.pop("mimetypes", None)
#         self._not_mimetypes = kwargs.pop("ignored_mimetypes", None)
#         super().__init__(*args, **kwargs)

#     @staticmethod
#     def _get_header(scope: Scope, name: bytes) -> str | None:
#         for key, value in scope["headers"]:
#             if key == name:
#                 return value.decode()
#         return ""

#     def _matches_mimetype(self, scope: Scope) -> bool:
#         if scope["method"] in ["GET", "HEAD"]:
#             if accepted_types := self._get_header(scope, b"accept"):
#                 if self._not_mimetypes and any(t in accepted_types for t in self._not_mimetypes):
#                     # TODO Routing - Find a better way to handle bypassed mimetypes
#                     return False
#                 return self._mimetypes is None or mimeparse.best_match(
#                     self._mimetypes, accepted_types
#                 )
#             raise HTTPException(400, "No accept header")
#         elif content_type := self._get_header(scope, b"content-type"):
#             return self._mimetypes is None or content_type in self._mimetypes
#         else:
#             raise HTTPException(400, "No content-type header")

#     def matches(self, scope: Scope) -> tuple[Match, Scope]:
#         return super().matches(scope) if self._matches_mimetype(scope) else (Match.NONE, scope)


# def _rdf_search(store: RdfResourceStore) -> HttpResponse:
#     # TODO RDF - support named graphs for search
#     search_engine = RdfSearchEngine(store.graph)
#     search_engine.add_index(
#         IndexedResource(
#             "https://www.w3.org/ns/activitystreams#Note",
#             [
#                 "https://www.w3.org/ns/activitystreams#content",
#                 "https://www.w3.org/ns/activitystreams#summary",
#             ],
#             [
#                 "https://www.w3.org/ns/activitystreams#content",
#                 "https://www.w3.org/ns/activitystreams#summary",
#             ],
#         )
#     )
#     search_engine.add_index(
#         IndexedResource(
#             "https://www.w3.org/ns/activitystreams#Person",
#             [
#                 "https://www.w3.org/ns/activitystreams#summary",
#             ],
#             [
#                 "https://www.w3.org/ns/activitystreams#summary",
#                 "https://www.w3.org/ns/activitystreams#name",
#                 "https://www.w3.org/ns/activitystreams#preferredUsername",
#             ],
#         )
#     )
#     log.info("Indexing RDF store")
#     search_engine.update_index()

#     def _search(request: HttpRequest) -> HttpResponse:
#         q = request.query_params["q"]
#         return JSONResponse(
#             search_engine.search(q[0] if q else ""),
#             headers={"Access-Control-Allow-Origin": "*"},
#         )

#     return _search


def _search_window(options: dict[str, list[str]], default: int) -> tuple[int, int]:
    """The ``start``/``limit`` a search asks for, clamped to what a store will serve."""
    start = 0
    if raw_start := option_value(options, "start"):
        try:
            start = max(int(raw_start), 0)
        except ValueError:
            raise ValueError(f"Malformed start: {raw_start}") from None
    limit = default
    if raw_limit := option_value(options, "limit"):
        try:
            limit = int(raw_limit)
        except ValueError:
            raise ValueError(f"Malformed limit: {raw_limit}") from None
        if limit < 1:
            raise ValueError("limit must be at least 1")
    return start, min(limit, MAX_SEARCH_PAGE_SIZE)


async def search_endpoint(
    request: Request, principal: Identity = Depends(get_principal)
) -> Response:
    """Search this tenant's public documents, as the requesting principal may see them."""
    if principal is None:
        raise HTTPException(HTTPStatus.UNAUTHORIZED, detail="Authentication required")

    tenant: Tenant = request.state.tenant
    config: ServerConfig = request.app.state.config
    query = option_value(request.query_params, "q") or ""
    try:
        start, limit = _search_window(request.query_params, DEFAULT_SEARCH_PAGE_SIZE)
    except ValueError as e:
        raise HTTPException(HTTPStatus.BAD_REQUEST, detail=str(e)) from None

    page = await tenant.public_store.search(
        query, text_fields=config.search.text_fields, start=start, limit=limit
    )
    authorizer = request.app.state.authorizer
    documents = [
        document
        for document in page.documents
        if (await authorizer.is_get_authorized(tenant, principal, document)).authorized
    ]

    return JSONResponse(
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "type": "OrderedCollection",
            "id": f"{tenant.prefix}/search?q={query}",
            "totalItems": page.total,
            "orderedItems": documents,
        },
        headers={
            "Access-Control-Allow-Origin": "*",
            "Content-Type": "application/activity+json",
        },
    )


class JsonSchemaValidator(Validator):
    def __init__(self, config: ServerConfig):
        self._validator = create_validator(
            root_schema=config.validation.root_schema,
            schema_dirs=[Path(d) for d in config.validation.schema_dirs],
            package_names=["firm.server.schemas"] + config.validation.package_names,
        )

    def validate(self, obj: JSONObject) -> None:
        try:
            self._validator.validate(obj)
        except ValidationError as e:
            log.error("Validation error: %s", e.message)
            raise HTTPException(400, detail=e.message)


# def with_middleware(
#     handler: Callable[[Request], Awaitable[Response]],
#     *middlewares: Middleware,
# ) -> Callable[[Request], Awaitable[Response]]:
#     """Wraps a handler with one or more Starlette Middleware instances, outermost first.
#     Builds the ASGI middleware chain once at setup time.
#     """

#     async def handler_app(scope, receive, send):
#         """Bridge: converts a Request->Response handler into an ASGI (scope, receive, send) app."""
#         scope["auth"] = scope["user"] # FIXME HACK!
#         request = Request(scope, receive, send)
#         response = lambda scope, receive, send: handler(request)
#         await response(scope, receive, send)

#     # Build the chain once — not per-request
#     app = handler_app
#     for middleware in reversed(middlewares):
#         app = middleware.cls(app, **middleware.kwargs)

#     async def wrapped(request: Request) -> Response:
#         response_parts: dict = {}

#         async def send_interceptor(message):
#             if message["type"] == "http.response.start":
#                 response_parts["status"] = message["status"]
#                 response_parts["headers"] = message.get("headers", [])
#             elif message["type"] == "http.response.body":
#                 response_parts["body"] = message.get("body", b"")

#         await app(request.connection.scope, request.connection.receive, send_interceptor)

#         return Response(
#             content=response_parts.get("body", b""),
#             status_code=response_parts.get("status", 200),
#             headers={k.decode(): v.decode() for k, v in response_parts.get("headers", [])},
#         )

#     return wrapped


RequestPredicate = Callable[[Request], bool]
RequestHandler = Callable[[Request], Awaitable[Response]]
AllowedMethods = list[str] | tuple[str, ...]

_AS2_CONTENT_TYPES = set(AS2_CONTENT_TYPES)


def accepts_activitypub(request: Request) -> bool:
    """Returns True only if the accept header explicitly names an ActivityPub MIME type.
    Wildcard types like */* are ignored so browser requests route to HTML."""
    accepted = request.headers.get("accept", "")
    if accepted in _AS2_CONTENT_TYPES:
        return True
    split_header = [h for h in accepted.split(",") if h]
    parsed_header = [mimeparse.parse_media_range(r) for r in split_header]
    explicit = {f"{t}/{s}" for t, s, _ in parsed_header if t != "*" and s != "*"}
    return bool(explicit & _AS2_CONTENT_TYPES)


def has_content_type(request, content_types):
    content_type = request.headers.get("content-type", "").strip()
    return content_type in content_types


@dataclass
class RoutingRule:
    predicate: RequestPredicate
    handler: RequestHandler
    allowed_methods: AllowedMethods = ("GET",)

    async def matches(self, request: Request) -> bool:
        return self.predicate(request) and request.method in self.allowed_methods


class DynamicRouter:
    """Dispatches a request to the first handler whose predicate matches."""

    def __init__(self, rules: list[RoutingRule] | None = None):
        self._rules: list[RoutingRule] = rules or []
        self._default: RequestHandler | None = None
        self._is_coroutine = asyncio.coroutines._is_coroutine  # type: ignore[attr-defined]

    def register(
        self,
        rule: RoutingRule,
    ) -> "DynamicRouter":
        self._rules.append(rule)
        return self

    def default(self, handler: RequestHandler) -> "DynamicRouter":
        self._default = handler
        return self

    async def __call__(
        self, request: Request, principal: Principal = Depends(get_principal)
    ) -> Response:
        request.scope["user"] = principal
        for rule in self._rules:
            if rule.predicate(request) and request.method in rule.allowed_methods:
                log.debug(
                    "Dispatching %s %s via %s",
                    request.method,
                    request.url.path,
                    rule.handler.__name__ if hasattr(rule.handler, "__name__") else rule.handler,
                )
                return await rule.handler(request)

        if self._default is not None:
            return await self._default(request)

        log.warning("No handler matched for %s %s", request.method, request.url.path)
        return Response(status_code=406)


async def nodeinfo_index_endpoint(request: Request) -> Response:
    data = await nodeinfo_index(str(request.base_url))
    return JSONResponse(data, media_type="application/jrd+json")


async def nodeinfo_version_endpoint(request: Request, version: str) -> Response:
    tenant = request.state.tenant
    try:
        return JSONResponse(await nodeinfo_version(tenant, version))
    except UnsupportedNodeInfoVersion as e:
        raise HTTPException(HTTPStatus.BAD_REQUEST, detail=str(e))


async def webfinger_endpoint(request: Request) -> Response:
    resource_params: list[str] | None = get_query_params(request.url).get("resource")
    if resource_params is None or len(resource_params) == 0:
        raise HTTPException(
            HTTPStatus.BAD_REQUEST,
            detail="Missing resource_uri param",
        )
    if len(resource_params) > 1:
        raise HTTPException(
            HTTPStatus.BAD_REQUEST,
            detail="Multiple resource_uri params not supported",
        )
    resource_uri = resource_params[0]
    try:
        data = await webfinger(request.state.tenant, resource_uri)
        return JSONResponse(data, media_type="application/jrd+json")
    except InvalidResourceUri as e:
        raise HTTPException(HTTPStatus.BAD_REQUEST, detail=str(e))
    except ResourceNotFound as e:
        raise HTTPException(HTTPStatus.NOT_FOUND, detail=str(e))


def proxy_endpoint(request: Request, principal: Principal = Depends(get_principal)) -> Response:
    if not principal:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Authentication required")
    return proxy(request)


def _delivery_json(row: Delivery) -> JSONObject:
    return {
        "id": row.id,
        "tenant": row.tenant,
        "activity": row.activity,
        "endpoint": row.endpoint,
        "state": row.state.value,
        "attempts": row.attempts,
        "next_attempt_at": row.next_attempt_at,
        "last_reason": row.last_reason,
        "created_at": row.created_at,
    }


def create_admin_router() -> APIRouter:
    """The delivery queue's admin surface: instance-wide, so it needs the role."""
    router = APIRouter(prefix="/admin")

    @router.get("/deliveries")
    async def list_deliveries(
        request: Request,
        principal: Principal = Depends(require_admin),
        state: DeliveryState | None = None,
        limit: int = 50,
    ) -> JSONResponse:
        queue = request.app.state.delivery_queue
        rows = await queue.list(state=state, limit=limit)
        return JSONResponse(
            {
                "counts": await queue.counts(),
                "deliveries": [_delivery_json(row) for row in rows],
            }
        )

    @router.post("/deliveries/{delivery_id}/retry", status_code=HTTPStatus.ACCEPTED)
    async def retry_delivery(
        request: Request,
        delivery_id: str,
        principal: Principal = Depends(require_admin),
    ) -> JSONResponse:
        queue = request.app.state.delivery_queue
        row = await queue.get(delivery_id)
        if row is None:
            raise HTTPException(HTTPStatus.NOT_FOUND, detail=f"no delivery {delivery_id}")
        await queue.reschedule(delivery_id, next_attempt_at=time.time(), reason="retry")
        due = await queue.get(delivery_id)
        if due is not None:
            await request.app.state.delivery_service.run(due)
        after = await queue.get(delivery_id)
        return JSONResponse(_delivery_json(after or row), status_code=HTTPStatus.ACCEPTED)

    @router.delete("/deliveries/{delivery_id}")
    async def cancel_delivery(
        request: Request,
        delivery_id: str,
        principal: Principal = Depends(require_admin),
    ) -> JSONResponse:
        queue = request.app.state.delivery_queue
        row = await queue.get(delivery_id)
        if row is None:
            raise HTTPException(HTTPStatus.NOT_FOUND, detail=f"no delivery {delivery_id}")
        await queue.abandon(delivery_id, reason="cancelled")
        after = await queue.get(delivery_id)
        return JSONResponse(_delivery_json(after or row))

    return router


def create_router(
    config: ServerConfig, delivery_service: DeliveryService | None = None
) -> APIRouter:
    router = APIRouter()

    router.add_api_route("/.well-known/nodeinfo", nodeinfo_index_endpoint, methods=["GET"])
    router.add_api_route("/nodeinfo/{version}", nodeinfo_version_endpoint, methods=["GET"])
    router.add_api_route("/.well-known/webfinger", webfinger_endpoint, methods=["GET"])
    router.add_api_route("/proxy", proxy_endpoint, methods=["POST"])
    router.add_api_route("/static/{file_path:path}", html_static_endpoint, methods=["GET"])

    router.include_router(create_oauth2_router())

    if config.search.enabled:
        router.add_api_route("/search", search_endpoint, methods=["GET"])

    router.add_api_route("/sse/client", sse_client_page_endpoint, methods=["GET"])
    router.include_router(create_sse_router())
    router.include_router(create_media_upload_router())
    router.include_router(create_admin_router())

    @router.get("/sse/test")
    async def sse_test_endpoint(
        request: Request,
        notifier: StreamNotifier = Depends(get_notifier),
    ) -> dict:
        topic = "test-topic"
        tenant = request.state.tenant
        actor = await tenant.public_store.query_one(
            {"type": "Person", "preferredUsername": "eric80"}
        )
        if actor:
            message = {"id": actor["id"], "summary": actor["summary"]}
        else:
            message = {"content": "No actor found"}
        await notifier.notify(
            topic,
            StreamEvent(
                id=uuid.uuid4().hex,
                topic=topic,
                type="Update",
                published=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                payload=message,
            ),
        )
        return {"status": "ok"}

    validator = JsonSchemaValidator(config)

    activitypub_service = ActivityPubService(
        authorizer=CoreAuthorizationService(),
        delivery_service=delivery_service or FirmDeliveryService(config, validator=validator),
        validator=validator,
    )

    async def activitypub_endpoint(request: Request) -> Response:
        if request.method in ["GET", "HEAD"]:
            try:
                context = FirmContext(
                    request.app.state.resolver,
                    request.state.tenant,
                    request.scope["user"],
                    request.app.state.authorizer,
                )
                resource = await activitypub_service.process_get(
                    context, request.url, request.query_params
                )
                status_code = 200
                if resource.get("type") == "Tombstone":
                    status_code = HTTPStatus.GONE
                return JSONResponse(
                    resource,
                    status_code=status_code,
                    headers={"Content-Type": "application/activity+json"},
                )
            except NotFoundException as e:
                raise HTTPException(HTTPStatus.NOT_FOUND, detail=str(e))
            except NotAuthorizedException as e:
                raise HTTPException(HTTPStatus.FORBIDDEN, detail=str(e))
            except InvalidRequestException as e:
                raise HTTPException(HTTPStatus.BAD_REQUEST, detail=str(e))
            except InvalidResourceTypeException as e:
                raise HTTPException(HTTPStatus.BAD_REQUEST, detail=str(e))
            except ResourceOwnerException as e:
                raise HTTPException(HTTPStatus.BAD_REQUEST, detail=str(e))
        elif request.method == "POST":
            try:
                context = FirmContext(
                    request.app.state.resolver,
                    request.state.tenant,
                    request.scope["user"],
                    request.app.state.authorizer,
                )
                create_resource_uri = await activitypub_service.process_post(
                    context, request.url, await request.json()
                )
                if create_resource_uri:
                    headers = {
                        "Access-Control-Expose-Headers": "Location",
                        "Location": create_resource_uri,
                    }
                    return PlainTextResponse(
                        "Created", status_code=HTTPStatus.CREATED, headers=headers
                    )
                else:
                    return PlainTextResponse("OK", media_type="text/plain")
            except NotFoundException as e:
                raise HTTPException(HTTPStatus.NOT_FOUND, detail=str(e))
            except NotAuthorizedException as e:
                raise HTTPException(HTTPStatus.FORBIDDEN, detail=str(e))
            except InvalidRequestException as e:
                raise HTTPException(HTTPStatus.BAD_REQUEST, detail=str(e))
            except InvalidResourceTypeException as e:
                raise HTTPException(HTTPStatus.BAD_REQUEST, detail=str(e))
            except ResourceOwnerException as e:
                raise HTTPException(HTTPStatus.FORBIDDEN, detail=str(e))
        else:
            raise HTTPException(HTTPStatus.METHOD_NOT_ALLOWED)

    router.add_api_route(
        "/{path:path}",
        DynamicRouter(
            [
                RoutingRule(
                    predicate=lambda req: not accepts_activitypub(req),
                    handler=html_endpoint,
                    allowed_methods=["GET", "HEAD"],
                ),
                RoutingRule(
                    predicate=lambda req: accepts_activitypub(req)
                    or has_content_type(req, AS2_CONTENT_TYPES),
                    # handler=_adapt_endpoint(activitypub_service.process_request),
                    handler=activitypub_endpoint,
                    allowed_methods=["GET", "HEAD", "POST"],
                ),
            ]
        ),
        methods=["GET", "HEAD", "POST"],
    )

    #     if isinstance(store, RdfResourceStore):
    #         log.info("Registering SPARQL endpoint")
    #         example_query = """\
    # PREFIX as: <https://www.w3.org/ns/activitystreams#>
    # PREFIX firm: <https://firm.core.stevebate.dev#>

    # SELECT ?object WHERE {
    #     ?object is as:Note
    # }
    # """.rstrip()
    #         # TODO SPARQL - tenant-specific config
    #         # The namespace will always be firm.core.stevebate.dev
    #         sparql_app = create_sparql_endpoint(
    #             "https://firm.core.stevebate.dev/sparql/",
    #             example_query=example_query,
    #             favicon="https://firm.core.stevebate.dev/static/favicon/favicon.ico",
    #         )
    #         routes.insert(4, Mount("/sparql", app=sparql_app, name="sparql"))
    #         # Add a search engine
    #         routes.insert(4, Route("/search", endpoint=_rdf_search(store)))d

    return router
