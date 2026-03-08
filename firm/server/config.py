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
    kind: StorageKind = StorageKind.MEMORY


@dataclass(frozen=True)
class ValidationConfig:
    root_schema: str = "schema:activities"
    schema_dirs: list[str] = field(default_factory=list)
    package_names: list[str] = field(default_factory=list)


@dataclass
class ServerConfig:
    tenants: list[str]
    # TODO files: str  # Storage for non-data files (media, etc.)
    store: FileStoreConfig | RdfStoreConfig | MemoryStoreConfig
    validation: ValidationConfig = ValidationConfig()
    shared_inbox_path: str = "shared"

    def is_local(self, uri: str) -> bool:
        return any(uri.startswith(tenant) for tenant in self.tenants)


def load_config(config_in: typing.IO | str) -> ServerConfig:
    def _load(config_stream: typing.IO):
        try:
            config_data = yaml.safe_load(config_stream)
            dacite_config = dacite.Config(
                type_hooks={
                    Path: Path,
                    StorageKind: StorageKind,
                }  # This tells dacite to convert strings to Path
            )
            return dacite.from_dict(data_class=ServerConfig, data=config_data, config=dacite_config)
        except dacite.exceptions.DaciteError as e:
            raise ServerException(f"Invalid configuration: {e}")

    if isinstance(config_in, str):
        with open(config_in) as f:
            return _load(f)
    else:
        return _load(config_in)
