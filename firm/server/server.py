import asyncio
import contextlib
import logging
from typing import cast
from urllib.parse import urlparse

import uvicorn
from click import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from firm.core.interfaces import FIRM_NS, JSONObject, ResourceStore, Tenant
from firm.server.config import (
    FileStoreConfig,
    MemoryStoreConfig,
    ServerConfig,
    StorageKind,
)
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


def init_remote_cache(config: ServerConfig) -> ResourceStore:
    match config.store.kind:
        case "filesystem":
            from firm.core.store.file import FileResourceStore

            filestore_config = cast(FileStoreConfig, config.store)
            return FileResourceStore(filestore_config.base / "cache")
        case "rdf":
            # init_tenant_rdf_storage(config, tenant_uri)
            raise NotImplementedError("RDF storage not supported yet")
        case "memory":
            from firm.core.store.memory import MemoryResourceStore

            return MemoryResourceStore()
    raise ValueError(
        f"Unknown storage kind: {config.store.kind}. " "Supported kinds are 'filesystem' and 'rdf'."
    )


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

        @contextlib.asynccontextmanager
        async def lifespan(app):
            log.info("ASGI lifespan: starting")
            app.state.config = config

            tenants = {}
            for tenant_uri in config.tenants:
                tenant = init_tenant(config, tenant_uri)
                tenants[tenant_uri] = tenant
                tenant_doc = await tenant.public_store.get(tenant_uri)
                if not tenant_doc:
                    await tenant.public_store.put(
                        {
                            "@context": "https://www.w3.org/ns/activitystreams",
                            "id": tenant_uri,
                            "type": ["Service", FIRM_NS.Tenant],
                        }
                    )

            app.state.tenants = tenants
            app.state.cache = init_remote_cache(config)

            yield

            log.info("ASGI lifespan: stopping")

        _app = FastAPI(lifespan=lifespan)
        _app.include_router(create_router(config))

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
