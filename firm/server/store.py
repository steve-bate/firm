# import logging
# import os
# from abc import ABC, abstractmethod
# from typing import final
# from urllib.parse import urlparse

# from firm.core.interfaces import ResourceStore
# from firm.core.store.file import FileResourceStore
# from firm.core.store.prefixstore import (
#     PrefixAwareResourceStore,
#     PrefixAwareResourceStoreWithFetch,
# )
# from firm_ld.store import RdfDataSet, RdfResourceStore

# from firm.server.adapters import HttpxTransport
# from firm.server.config import FileStoreConfig, ServerConfig, StorageKind
# from firm.server.exceptions import ServerException

# log = logging.getLogger(__name__)


# class StoreDriver(ABC):
#     def __init__(self, name: str) -> None:
#         self.name = name
#         self._store = None

#     @property
#     def store(self) -> ResourceStore:
#         if not self._store:
#             raise ServerException("Resource store not initialized", logging.CRITICAL)

#         return self._store

#     @final
#     def open(self, config: ServerConfig) -> ResourceStore:
#         self._store = self._open(config)
#         return self._store

#     @abstractmethod
#     def _open(self, config: ServerConfig) -> ResourceStore:
#         ...

#     @final
#     def close(self):
#         if self._store and hasattr(self.store, "close"):
#             self._store.close()


# class RdfStoreDriver(StoreDriver):
#     def __init__(self) -> None:
#         super().__init__("rdf")

#     def _open(self, config: ServerConfig) -> ResourceStore:
#         if not config.store.kind == StorageKind.RDF:
#             raise ServerException("RDF store configuration missing", logging.CRITICAL)
#         graph_path = config.store.graph_path
#         log.info("Opening RDF graph store at %s", graph_path)
#         RdfDataSet.configure("Oxigraph", [graph_path])
#         return RdfResourceStore(RdfDataSet.VALUE)
