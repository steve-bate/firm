import typing
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import dacite
import yaml

from firm.server.exceptions import ServerException


class StorageKind(StrEnum):
    FILESYSTEM = "filesystem"
    RDF = "rdf"
    MEMORY = "memory"  # for testing
    SQLITE = "sqlite"


class CacheKind(StrEnum):
    MEMORY = "memory"
    SQLITE = "sqlite"


@dataclass(frozen=True)
class FileStoreConfig:
    base: Path
    kind: StorageKind = StorageKind.FILESYSTEM


@dataclass(frozen=True)
class RdfStoreConfig:
    graph_path: Path
    kind: StorageKind = StorageKind.RDF


@dataclass(frozen=True)
class MemoryStoreConfig:
    files: Path
    kind: StorageKind = StorageKind.MEMORY


@dataclass(frozen=True)
class SqliteStoreConfig:
    """Where the per-tenant databases live.

    Same shape as FileStoreConfig on purpose: the tenant layout is the same, and the
    ``kind`` value is what chooses between them.
    """

    base: Path
    kind: StorageKind = StorageKind.SQLITE


@dataclass(frozen=True)
class ValidationConfig:
    root_schema: str = "schema:activities"
    schema_dirs: list[str] = field(default_factory=list)
    package_names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RemoteFetchConfig:
    enabled: bool = True
    kind: CacheKind | None = None
    path: Path | None = None
    ttl: float = 300.0
    failure_ttl: float = 60.0
    not_found_ttl: float = 86_400.0
    max_bytes: int = 1_048_576
    timeout: float = 5.0
    # Kept equal to firm.core.remote.cache.DEFAULT_MAX_ENTRIES by hand: config cannot
    # import the core (the cycle closes through firm.core.interfaces).
    max_entries: int = 10_000
    allow_private_addresses: bool = False
    signing_actor: str | None = None


@dataclass(frozen=True)
class DeliveryConfig:
    """Outbound delivery: how long to wait, how many tries, how far to expand.

    ``timeout`` repeats firm.core.interfaces.DEFAULT_HTTP_TIMEOUT because this module
    must not import the core: the import closes the cycle through interfaces.
    """

    timeout: float = 5.0
    max_attempts: int = 3
    retry_backoff: float = 0.5
    retry_backoff_max: float = 3_600.0
    max_age: float = 86_400.0
    max_expand_depth: int = 2
    concurrency: int = 8
    # The queue and its worker: where the work lives, and how often it is looked at.
    queue_path: Path | None = None
    worker_interval: float = 5.0
    lease: float = 300.0
    history_ttl: float = 3_600.0
    batch: int = 50


@dataclass(frozen=True)
class TenantConfig:
    prefix: str


@dataclass
class ServerConfig:
    tenants: list[TenantConfig]
    # TODO files: str  # Storage for non-data files (media, etc.)
    store: FileStoreConfig | RdfStoreConfig | MemoryStoreConfig | SqliteStoreConfig
    validation: ValidationConfig = ValidationConfig()
    shared_inbox_path: str = "shared"
    remote_fetch: RemoteFetchConfig = field(default_factory=RemoteFetchConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)

    def is_local(self, uri: str) -> bool:
        return any(uri.startswith(tenant.prefix) for tenant in self.tenants)


def load_config(config_in: typing.IO | str) -> ServerConfig:
    def _load(config_stream: typing.IO):
        try:
            config_data = yaml.safe_load(config_stream)
            dacite_config = dacite.Config(
                type_hooks={
                    Path: Path,
                    StorageKind: StorageKind,
                    CacheKind: CacheKind,
                }  # This tells dacite to convert strings to Path and enums
            )
            return dacite.from_dict(data_class=ServerConfig, data=config_data, config=dacite_config)
        except dacite.exceptions.DaciteError as e:
            raise ServerException(f"Invalid configuration: {e}")

    if isinstance(config_in, str):
        with open(config_in) as f:
            return _load(f)
    else:
        return _load(config_in)
