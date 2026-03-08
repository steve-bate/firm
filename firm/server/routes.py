import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Mapping, cast

import httpx
import mimeparse
from jsonschema.exceptions import ValidationError
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.responses import PlainTextResponse as StarlettePlainTextResponse
from starlette.responses import Response
from starlette.routing import Match, Route, Scope

from firm.core.auth.authorization import CoreAuthorizationService
from firm.core.auth.bearer_token import BearerTokenAuthenticator
from firm.core.auth.chained import AuthenticatorChain
from firm.core.auth.http_signature import HttpSigAuthenticator, HttpSignatureAuth
from firm.core.interfaces import (
    FIRM_NS,
    DeliveryService,
    HttpException,
    HttpRequest,
    HttpResponse,
    JSONObject,
    JsonResponse,
    PlainTextResponse,
    ResourceStore,
    Tenant,
    Validator,
)
from firm.core.services.activitypub import ActivityPubService
from firm.core.services.nodeinfo import nodeinfo_index, nodeinfo_version
from firm.core.services.webfinger import webfinger
from firm.core.util import (
    AP_PUBLIC_URIS,
    AS2_CONTENT_TYPES,
    get_id,
    get_prefix_uri,
    get_types,
)
from firm.jsonschema.validation import create_validator
from firm.server.adapters import (
    AuthenticationBackendAdapter,
    HttpConnectionAdapter,
    HttpxAuthAdapter,
)
from firm.server.config import ServerConfig, StorageKind
from firm.server.html.endpoint import html_endpoint, html_static_endpoint

from .proxy import proxy

log = logging.getLogger(__name__)


def _adapt_response(r: HttpResponse) -> Response:
    if isinstance(r, JsonResponse):
        return JSONResponse(r.json, status_code=r.status_code, headers=r.headers)
    if isinstance(r, PlainTextResponse):
        return StarlettePlainTextResponse(r.content, status_code=r.status_code, headers=r.headers)
    return Response(status_code=r.status_code, headers=r.headers, content=r.body)


def _adapt_endpoint(
    method: Callable[[HttpRequest], Awaitable[HttpResponse]],
    authenticated=False,
) -> Response:
    async def wrapper(request: Request):
        try:
            if authenticated and not request.user.is_authenticated:
                raise HTTPException(401)
            return _adapt_response(await method(HttpConnectionAdapter(request)))
        except HttpException as e:
            raise HTTPException(e.status_code, detail=e.detail, headers=e.headers)

    return wrapper


# TODO Move is_collection to util (maybe firm core)
def is_collection(obj: JSONObject) -> bool:
    return obj.get("type") in ["Collection", "OrderedCollection"]


def is_public(uri: str):
    return uri in AP_PUBLIC_URIS


def _get_uris(items: list[JSONObject | str]) -> list[str]:
    uris: list[str] = []
    for item in items:
        if isinstance(item, str):
            uris.append(item)
        elif isinstance(item, dict) and "id" in item:
            uris.append(cast(str, item["id"]))
    return uris


# TODO Consider redesign of FirmDeliveryService (abstract class?)
class FirmDeliveryService(DeliveryService):
    _RECIPIENT_PROPS = ["to", "cc", "bto", "bcc"]

    def __init__(self, config: ServerConfig):
        self._config = config

    async def _resolve_inboxes(
        self,
        store: ResourceStore,
        recipient_uris: Iterable[str],
    ) -> set[str]:
        inboxes = set()
        for uri in recipient_uris:
            if is_public(uri):
                continue
            obj = await store.get(uri)
            if not obj:
                continue
            if is_collection(obj):  # TODO Expand server-local collections for inboxes
                # ... and self._store.is_local(uri):
                if items := cast(
                    list[JSONObject | str], obj.get("items") or obj.get("orderedItems")
                ):
                    for item in await self._resolve_inboxes(store, _get_uris(items)):
                        inboxes.add(item)
            else:
                if inbox_uri := obj.get("sharedInbox") or obj.get("inbox"):
                    inboxes.add(cast(str, inbox_uri))
        return inboxes

    def _remove_keys(self, obj: JSONObject, predicate: Callable[[str], bool]) -> None:
        for key, value in obj.items():
            if key.startswith("firm:"):
                obj.pop(key)
            else:
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            self._remove_keys(item, predicate)
                else:
                    if isinstance(value, dict):
                        self._remove_keys(value, predicate)

    async def _post(
        self,
        inbox: str,
        /,
        message: JSONObject,
        auth: HttpSignatureAuth,
    ) -> None:
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    inbox,
                    json=message,
                    headers={"Content-Type": "application/activity+json"},
                    auth=HttpxAuthAdapter(auth),
                )
            except Exception as e:
                log.error(f"Error posting to {inbox}: {e}")
                return
            log.info(
                f"FirmDeliveryService POST {inbox} " f"{response.status_code} text={response.text}"
            )

    async def _serialize(self, tenant: Tenant, activity: JSONObject) -> JSONObject:
        message = cast(dict, activity).copy()
        message.pop("bto", None)
        message.pop("bcc", None)
        self._remove_keys(message, lambda k: k.startswith("firm:"))
        # TODO Define message/property specific serialization
        # (selected object embedding, collection paging, etc.)
        if isinstance(activity.get("object"), str) and "Follow" not in get_types(activity):
            uri = get_id(activity["object"])
            if uri:
                obj = await tenant.public_store.get(uri)
                if obj:
                    message["object"] = obj
        return message

    async def deliver(
        self,
        tenant: Tenant,
        all_tenants: Mapping[str, Tenant],
        activity: JSONObject,
    ) -> None:
        # TODO Delivery - Handle failures and redelivery
        actor = await tenant.public_store.get(cast(str, activity["actor"]))
        if not actor:
            log.error("Actor not found for activity: %s", activity["actor"])
            return
        key_uri = get_id(actor.get("publicKey", {}))
        if not key_uri:
            log.error("No key for actor %s", actor["id"])
            return
        credentials = await tenant.private_store.query_one(
            {
                "type": FIRM_NS.Credentials.value,
                "attributedTo": actor["id"],
            }
        )
        if not credentials:
            log.error("No credentials found for actor %s", actor["id"])
            return
        private_key_pem = cast(str, credentials.get(FIRM_NS.privateKey.value))
        if not private_key_pem:
            log.error("No private key found for actor %s", actor["id"])
            return
        auth = HttpSignatureAuth(key_uri, private_key_pem)
        recipient_uris: set[str] = set()
        for prop in self._RECIPIENT_PROPS:
            if r := activity.get(prop):
                if isinstance(r, str):
                    recipient_uris.add(r)
                elif isinstance(r, list):
                    recipient_uris.update(r)
        inboxes = await self._resolve_inboxes(tenant.public_store, recipient_uris)
        message = None
        for inbox_uri in inboxes:
            if self._config.is_local(inbox_uri):
                inbox_prefix = get_prefix_uri(inbox_uri)
                target_tenant = (
                    tenant if inbox_prefix == tenant.prefix else all_tenants.get(inbox_prefix)
                )
                if not target_tenant:
                    log.error(f"Unknown tenant for inbox {inbox_uri}")
                    continue
                store = target_tenant.public_store
                inbox = await store.get(inbox_uri)
                if not inbox:
                    log.error(f"Inbox not found: {inbox_uri}")
                    continue
                items = cast(list[JSONObject | str], inbox.get("orderedItems", []))
                items.insert(0, cast(str, activity["id"]))
                inbox["orderedItems"] = items
                await store.put(inbox)
            else:
                if message is None:
                    message = await self._serialize(tenant, activity)
                await self._post(inbox_uri, message=message, auth=auth)


class MimeTypeRoute(Route):
    def __init__(self, *args, **kwargs):
        self._mimetypes = kwargs.pop("mimetypes", None)
        self._not_mimetypes = kwargs.pop("ignored_mimetypes", None)
        super().__init__(*args, **kwargs)

    @staticmethod
    def _get_header(scope: Scope, name: bytes) -> str | None:
        for key, value in scope["headers"]:
            if key == name:
                return value.decode()
        return ""

    def _matches_mimetype(self, scope: Scope) -> bool:
        if scope["method"] in ["GET", "HEAD"]:
            if accepted_types := self._get_header(scope, b"accept"):
                if self._not_mimetypes and any(t in accepted_types for t in self._not_mimetypes):
                    # TODO Routing - Find a better way to handle bypassed mimetypes
                    return False
                return self._mimetypes is None or mimeparse.best_match(
                    self._mimetypes, accepted_types
                )
            raise HTTPException(400, "No accept header")
        elif content_type := self._get_header(scope, b"content-type"):
            return self._mimetypes is None or content_type in self._mimetypes
        else:
            raise HTTPException(400, "No content-type header")

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        return super().matches(scope) if self._matches_mimetype(scope) else (Match.NONE, scope)


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


@dataclass
class SearchTerm:
    operator: str | None
    value: str


async def _filesystem_search(request: HttpRequest) -> HttpResponse:
    tenant = request.state.tenant

    query = request.query_params.get("q", [""])[0].strip()

    term_pattern = r'(?:(\w+):)?(?:"([^"]+)"|(\S+))'
    terms = []
    for match in re.finditer(term_pattern, query):
        op, quoted, word = match.groups()
        terms.append(SearchTerm(op, (quoted if quoted is not None else word).lower()))

    prefix = str(request.base_url).rstrip("/")

    matches: dict[str, dict] = {
        "actors": {},
        "objects": {},
    }

    for term in terms:
        if term.operator is None or term.operator == "actor":
            actors = await tenant.public_store.query(
                {
                    "type": "Person",
                }
            )
            actor_matches = matches["actors"]
            for actor in actors:
                for p in ["name", "preferredUsername", "summary"]:
                    if term.value in cast(str, actor.get(p, "")).lower():
                        actor_matches[actor["id"]] = actor
                        if len(actor_matches) >= 20:
                            break
        if term.operator is None or term.operator == "object":
            # TODO Search - extend 'object' operator to support more types
            objects = await tenant.public_store.query(
                {
                    "type": "Note",
                }
            )
            object_matches = matches["objects"]
            for obj in objects:
                for p in ["name", "summary", "content"]:
                    if term.value in cast(str, obj.get(p, "")).lower():
                        object_matches[obj["id"]] = obj
                        if len(object_matches) >= 20:
                            break

    return JSONResponse(
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "type": "OrderedCollection",
            "id": f"{prefix}/search?q={query}",
            "totalItems": len(matches["actors"]) + len(matches["objects"]),
            "orderedItems": list(matches["actors"].values()) + list(matches["objects"].values()),
        },
        headers={"Access-Control-Allow-Origin": "*"},
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
            raise HttpException(400, e.message)


def get_routes(config: ServerConfig):
    validator = JsonSchemaValidator(config)
    activitypub_service = ActivityPubService(
        authorizer=CoreAuthorizationService(),
        delivery_service=FirmDeliveryService(config),
        validator=validator,
    )
    routes = [
        Route("/.well-known/webfinger", endpoint=_adapt_endpoint(webfinger)),
        Route("/.well-known/nodeinfo", endpoint=_adapt_endpoint(nodeinfo_index)),
        Route("/nodeinfo/{version}", endpoint=_adapt_endpoint(nodeinfo_version)),
        Route(
            "/proxy",
            endpoint=_adapt_endpoint(proxy),
            methods=["POST"],
            middleware=[
                Middleware(
                    AuthenticationMiddleware,
                    backend=AuthenticationBackendAdapter(
                        AuthenticatorChain(
                            [
                                BearerTokenAuthenticator(),
                                HttpSigAuthenticator(),
                            ]
                        ),
                    ),
                )
            ],
        ),
        Route("/static/{file_path:path}", endpoint=html_static_endpoint),
        MimeTypeRoute(
            "/{path:path}",
            endpoint=html_endpoint,
            ignored_mimetypes=AS2_CONTENT_TYPES,
        ),
        MimeTypeRoute(
            "/{path:path}",
            endpoint=_adapt_endpoint(activitypub_service.process_request),
            mimetypes=AS2_CONTENT_TYPES,
            methods=["GET", "POST"],
            middleware=[
                Middleware(
                    AuthenticationMiddleware,
                    backend=AuthenticationBackendAdapter(
                        AuthenticatorChain(
                            [
                                BearerTokenAuthenticator(),
                                HttpSigAuthenticator(),
                            ]
                        ),
                    ),
                )
            ],
        ),
    ]

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

    if config.store.kind == StorageKind.FILESYSTEM:
        log.info("Registering file system search")
        routes.insert(0, Route("/search", endpoint=_filesystem_search, methods=["GET"]))

    return routes
