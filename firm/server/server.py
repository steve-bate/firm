import asyncio
import contextlib
import logging
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from firm.core.auth.authorization import CoreAuthorizationService
from firm.core.auth.http_signature import HttpSignatureAuth
from firm.core.delivery.queue import DeliveryQueue, MemoryDeliveryQueue
from firm.core.delivery.sqlite import SqliteDeliveryQueue
from firm.core.interfaces import (
    FIRM_NS,
    HttpRequestSigner,
    JSONObject,
    ResourceStore,
    Tenant,
)
from firm.core.remote.cache import MemoryRemoteCache, RemoteCache, SqliteRemoteCache
from firm.core.remote.provider import FetchPolicy, RemoteResourceProvider
from firm.core.resolver import ResourceResolver
from firm.core.util import get_id
from firm.server.adapters import HttpxTransport
from firm.server.config import (
    CacheKind,
    FileStoreConfig,
    MemoryStoreConfig,
    ServerConfig,
    StorageKind,
)
from firm.server.delivery import FirmDeliveryService
from firm.server.delivery_worker import DeliveryWorker
from firm.server.routes import create_router

log = logging.getLogger(__name__ if __name__ != "__main__" else "firm.server.main")

_app = None


def init_tenant_file_storage(
    config: ServerConfig, tenant_uri: str
) -> tuple[ResourceStore, ResourceStore, Path]:
    from firm.core.store.file import FileResourceStore

    tenant_url = urlparse(tenant_uri)
    filestore_config = cast(FileStoreConfig, config.store)
    storage_prefix = filestore_config.base / "tenants" / tenant_url.netloc.replace(":", "_")
    public_store = FileResourceStore(storage_prefix / "public")
    private_store = FileResourceStore(storage_prefix / "private")
    files = storage_prefix / "files"
    return public_store, private_store, files


def init_tenant_stores(
    config: ServerConfig, tenant_uri: str
) -> tuple[ResourceStore, ResourceStore, Path]:
    match config.store.kind:
        case StorageKind.FILESYSTEM:
            return init_tenant_file_storage(config, tenant_uri)
        case StorageKind.MEMORY:
            from firm.core.store.memory import MemoryResourceStore

            memory_config = cast(MemoryStoreConfig, config.store)
            return (MemoryResourceStore(), MemoryResourceStore(), memory_config.files)
        case StorageKind.RDF:
            # init_tenant_rdf_storage(config, tenant_uri)
            raise NotImplementedError("RDF storage not supported yet")
        case _:
            raise ValueError(
                f"Unknown storage kind: {config.store.kind}. "
                "Supported kinds are 'filesystem' and 'rdf'."
            )


def init_tenant(config: ServerConfig, tenant_uri: str) -> Tenant:
    public_store, private_store, files = init_tenant_stores(config, tenant_uri)
    shared_inbox_uri = f"{tenant_uri}/{config.shared_inbox_path}"
    endpoints: JSONObject = {
        "sharedInbox": shared_inbox_uri,
    }
    return Tenant(tenant_uri, public_store, private_store, files, endpoints)


def _cache_dir(config: ServerConfig) -> Path:
    base = getattr(config.store, "base", None)
    return Path(base) / "cache" if base is not None else Path("cache")


def _default_cache_kind(config: ServerConfig) -> CacheKind:
    if config.store.kind == StorageKind.MEMORY:
        return CacheKind.MEMORY
    return CacheKind.SQLITE


def init_remote_cache(config: ServerConfig) -> RemoteCache:
    """The cache backend for fetched documents: memory follows a memory store."""
    remote_fetch = config.remote_fetch
    kind = remote_fetch.kind or _default_cache_kind(config)
    if kind == CacheKind.MEMORY:
        return MemoryRemoteCache(remote_fetch.max_entries)
    return SqliteRemoteCache(
        remote_fetch.path or _cache_dir(config) / "remote.sqlite", remote_fetch.max_entries
    )


def _delivery_path(config: ServerConfig) -> Path:
    base = getattr(config.store, "base", None)
    return Path(base) / "delivery.sqlite" if base is not None else Path("delivery.sqlite")


def init_delivery_queue(config: ServerConfig) -> DeliveryQueue:
    """The queue backend: memory follows a memory store, a file store gets a file."""
    if config.delivery.queue_path is not None:
        return SqliteDeliveryQueue(config.delivery.queue_path)
    if config.store.kind == StorageKind.MEMORY:
        return MemoryDeliveryQueue()
    return SqliteDeliveryQueue(_delivery_path(config))


def init_remote_provider(config: ServerConfig) -> RemoteResourceProvider | None:
    """The shared fetch provider, or None when remote fetching is off."""
    if not config.remote_fetch.enabled:
        return None
    remote_fetch = config.remote_fetch
    policy = FetchPolicy(
        ttl=remote_fetch.ttl,
        failure_ttl=remote_fetch.failure_ttl,
        not_found_ttl=remote_fetch.not_found_ttl,
        timeout=remote_fetch.timeout,
        max_bytes=remote_fetch.max_bytes,
        max_entries=remote_fetch.max_entries,
        allow_private_addresses=remote_fetch.allow_private_addresses,
    )
    return RemoteResourceProvider(init_remote_cache(config), HttpxTransport(), policy=policy)


async def init_remote_signer(
    config: ServerConfig, resolver: ResourceResolver
) -> HttpRequestSigner | None:
    """The signature for outbound GETs, or None when no signing actor is configured."""
    actor_uri = config.remote_fetch.signing_actor
    if not actor_uri:
        return None
    tenant = resolver.owner(actor_uri)
    if tenant is None:
        log.warning("Signing actor %s belongs to no tenant here; GETs are unsigned", actor_uri)
        return None
    actor = await tenant.public_store.get(actor_uri)
    key_uri = get_id(actor.get("publicKey", {})) if actor else ""
    credentials = await tenant.private_store.query_one(
        {"type": FIRM_NS.Credentials.value, "attributedTo": actor_uri}
    )
    private_key = credentials.get(FIRM_NS.privateKey.value) if credentials else None
    if not key_uri or not private_key:
        log.warning("No key for signing actor %s; GETs are unsigned", actor_uri)
        return None
    return HttpSignatureAuth(key_uri, cast(str, private_key))


def get_tenant_uri(scope):
    headers = {k.lower(): v for k, v in scope["headers"]}

    def get_header(name):
        return headers.get(name.encode("latin1"), b"").decode("latin1")

    scheme = get_header("x-forwarded-proto") or scope.get("scheme", "http")
    host = (
        get_header("x-forwarded-host")
        or get_header("host")
        or scope.get("server", ["localhost"])[0]
    )
    port = get_header("x-forwarded-port") or None
    prefix = get_header("x-forwarded-prefix") or scope.get("root_path", "")

    # If host header doesn't include port and x-forwarded-port is set, add it
    if ":" not in host and port:
        # Only add port if it's nonstandard for the scheme
        port = int(port)
        if (scheme == "http" and port != 80) or (scheme == "https" and port != 443):
            host = f"{host}:{port}"

    return f"{scheme}://{host}{prefix}"


class TenantMiddleware:
    def __init__(self, app: FastAPI):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            tenant_uri = get_tenant_uri(scope)
            app = scope["app"]
            if tenant := app.state.tenants.get(tenant_uri):
                scope["state"]["tenant"] = tenant
            else:
                raise HTTPException(
                    status_code=404,
                    detail=f"Unknown tenant: {tenant_uri}",
                )
        await self.app(scope, receive, send)


def app_factory(config: ServerConfig) -> FastAPI:
    global _app
    if _app is None:
        authorizer = CoreAuthorizationService()
        delivery_queue = init_delivery_queue(config)
        delivery_service = FirmDeliveryService(config, authorizer=authorizer, queue=delivery_queue)
        delivery_worker = DeliveryWorker(delivery_service, delivery_queue, config.delivery)

        @contextlib.asynccontextmanager
        async def lifespan(app):
            log.info("ASGI lifespan: starting")
            app.state.config = config

            tenants = {}
            for tenant in config.tenants:
                tenant = init_tenant(config, tenant.prefix)
                tenants[tenant.prefix] = tenant
                tenant_doc = await tenant.public_store.get(tenant.prefix)
                if not tenant_doc:
                    await tenant.public_store.put(
                        {
                            "@context": "https://www.w3.org/ns/activitystreams",
                            "id": tenant.prefix,
                            "type": ["Service", FIRM_NS.Tenant],
                        }
                    )

            app.state.tenants = tenants
            provider = init_remote_provider(config)
            app.state.resolver = ResourceResolver(tenants, provider)
            if provider is not None:
                provider.set_signer(await init_remote_signer(config, app.state.resolver))
            app.state.authorizer = authorizer
            app.state.delivery_service = delivery_service
            app.state.delivery_queue = delivery_queue
            app.state.delivery_worker = delivery_worker
            delivery_service.set_resolver(app.state.resolver)
            log.info("Remote cache: purged %d expired entries", await app.state.resolver.purge())

            # One pass in the startup path: the queue opens here rather than inside a
            # cancellable task, and work left by the previous run starts immediately.
            try:
                await delivery_worker.tick()
            except Exception:
                log.exception("Delivery worker: the first pass failed")
            worker_task = asyncio.create_task(delivery_worker.serve())
            log.info("Delivery worker: started, every %ss", config.delivery.worker_interval)

            yield

            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
            await delivery_queue.close()
            await app.state.resolver.close()

            log.info("ASGI lifespan: stopping")

        _app = FastAPI(lifespan=lifespan)
        _app.include_router(create_router(config, delivery_service))

        log.info("Adding CORS middleware")

        _app.add_middleware(TenantMiddleware)

        _app.add_middleware(
            CORSMiddleware,
            allow_origins=(config.cors_origins if hasattr(config, "cors_origins") else ["*"]),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    return _app


def clear_app():
    "Test suport to clear global app state"
    global _app
    _app = None


class FirmServer(uvicorn.Server):
    tasks: list[asyncio.Task] = []

    """Customized uvicorn.Server
    Uvicorn server overrides signals """

    def handle_exit(self, sig: int, frame) -> None:
        # Stop all tasks
        for task in self.tasks:
            if task != self:
                task.cancel()
        return super().handle_exit(sig, frame)


async def async_run(config: ServerConfig, verbose: bool, kwargs) -> None:
    def app_factory_with_context() -> FastAPI:
        return app_factory(config)

    try:
        logging.getLogger("uvicorn.error").name = "uvicorn"
        server = FirmServer(
            config=uvicorn.Config(
                app_factory_with_context,
                factory=True,
                log_config=None,
                forwarded_allow_ips="*",
                **kwargs,
            )
        )
        server.tasks = list(
            map(
                asyncio.create_task,
                [server.serve()],
            )
        )
        done, _ = await asyncio.wait(server.tasks)
        for d in done:
            if d.exception() is not None:
                raise d.exception()  # type: ignore
    finally:
        log.info("Server shutdown")
        try:
            await server.shutdown()
        except:  # noqa
            pass
        logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)


def run(config: ServerConfig, verbose: bool, kwargs):
    asyncio.run(async_run(config, verbose, kwargs))
