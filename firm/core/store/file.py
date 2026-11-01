import glob
import json
import logging
from collections.abc import Sequence
from functools import lru_cache
from os import PathLike
from pathlib import Path
from typing import Any, cast

from firm.core.interfaces import (
    DEFAULT_COLLECTION_PAGE_SIZE,
    DEFAULT_SEARCH_PAGE_SIZE,
    DEFAULT_TEXT_FIELDS,
    CollectionPage,
    JSONObject,
    QueryCriteria,
    SearchPage,
)
from firm.core.store import file_collections
from firm.core.store.base import ResourceStoreBase
from firm.core.store.naming import uri_hash
from firm.core.util import normalize_collection_items
from firm.search.transform.json import JsonMatcher

log = logging.getLogger(__name__)


class FileResourceStore(ResourceStoreBase):  # type: ignore
    def __init__(self, store_path: PathLike[Any], partition_name: str = ""):
        self.store_path = store_path if isinstance(store_path, Path) else Path(store_path)
        self._partition_path = self.store_path / partition_name
        self._partition_path.mkdir(parents=True, exist_ok=True)
        log.debug("FileResourceStore initialized at '%s'", self._partition_path)

    def __repr__(self) -> str:
        return f"FileResourceStore({self.store_path})"

    def __str__(self) -> str:
        return self.__repr__()

    @lru_cache
    def _hash(self, uri: str) -> str:
        return uri_hash(uri)

    def _filepath(self, uri: str) -> Path:
        return self._partition_path / f"{self._hash(uri)}.json"

    async def get(self, uri: str) -> JSONObject | None:
        """Retrieve Object based on uri"""
        filepath = self._filepath(uri)
        if filepath.exists():
            with open(filepath) as fp:
                return cast(JSONObject, json.load(fp))
        return None

    async def is_stored(self, uri: str) -> bool:
        filepath = self._filepath(uri)
        return filepath.exists()

    async def put(self, resource: JSONObject) -> None:
        """Store an AP Object"""
        normalize_collection_items(resource)
        if "id" not in resource:
            raise ValueError("Resource must have an 'id' property")
        with open(self._filepath(str(resource["id"])), "w") as fp:
            fp.write(json.dumps(resource, indent=2))

    async def remove(self, uri: str) -> None:
        """Remove an object from the store"""
        self._filepath(uri).unlink(True)

    async def query(self, criteria: QueryCriteria) -> list[JSONObject]:
        matches: list[JSONObject] = []
        for filename in glob.glob(f"{self._partition_path}/*.json"):
            with open(filename) as fp:
                data = json.load(fp)
                if self.is_match(data, criteria):
                    matches.append(data)
        return matches

    async def search(
        self,
        query: str,
        *,
        text_fields: Sequence[str] = DEFAULT_TEXT_FIELDS,
        start: int = 0,
        limit: int = DEFAULT_SEARCH_PAGE_SIZE,
    ) -> SearchPage:
        """Match every document in this partition, keeping only the window asked for.

        The walk is the same one ``query`` does, but documents outside the window are
        dropped as they are read instead of being held, so a search over a large partition
        costs one document at a time. Filenames are sorted so a window is stable across
        calls.
        """
        matcher = JsonMatcher(query, text_fields=list(text_fields))
        matched = 0
        documents: list[JSONObject] = []
        for filename in sorted(glob.glob(f"{self._partition_path}/*.json")):
            with open(filename) as fp:
                document = json.load(fp)
            if not matcher.is_match(document):
                continue
            if start <= matched < start + limit:
                documents.append(document)
            matched += 1
        return SearchPage(documents=tuple(documents), total=matched, start_index=start)

    async def append_item(self, collection_uri: str, item_uri: str) -> int:
        return await file_collections.append_item(self, collection_uri, item_uri)

    async def remove_item(self, collection_uri: str, item_uri: str) -> int:
        return await file_collections.remove_item(self, collection_uri, item_uri)

    async def contains_item(self, collection_uri: str, item_uri: str) -> bool:
        return await file_collections.contains_item(self, collection_uri, item_uri)

    async def count_items(self, collection_uri: str) -> int:
        return await file_collections.count_items(self, collection_uri)

    async def page_items(
        self,
        collection_uri: str,
        cursor: str | None = None,
        *,
        start: int | None = None,
        limit: int = DEFAULT_COLLECTION_PAGE_SIZE,
    ) -> CollectionPage:
        return await file_collections.page_items(
            self, collection_uri, cursor, start=start, limit=limit
        )

    async def close(self) -> None:
        pass


# #
# # CLI Support
# #


# @click.group("filestore")
# def filestore_cli() -> None:
#     """File-based resource storage tools"""


# @filestore_cli.command("id")
# @click.argument("uri")
# def uri_id(uri: str) -> None:
#     """Generate the id for a URI."""
#     print(uri_hash(uri))


# @filestore_cli.command
# @click.pass_context
# @click.option("--partition", help="Target partition")
# @click.argument("filepaths", nargs=-1)
# @async_command
# async def load(ctx, partition_name: str | None, filepaths: list[str]):
#     """Load resource files into the store"""
#     try:
#         actrill_ctx = ctx.obj["ACTRILL_CONTEXT"]
#         storage_partition: FileResourcePartition = actrill_ctx.store.get_partition(
#             partition_name
#         )
#         for filepath in filepaths:
#             await storage_partition.load_resources(filepath)
#     except Exception as ex:
#         raise click.ClickException(ex.args[0])


# @filestore_cli.command
# @click.option("--verbose", is_flag=True, help="Verbose output")
# @click.argument("filepaths", nargs=-1)
# @async_command
# async def rename(verbose: bool, filepaths: list[str]):
#     """Renames files based on the resource ID"""
#     try:
#         for filepath in filepaths:
#             if ResourcePartitionBase.is_json_file(filepath):
#                 with open(filepath) as fp:
#                     data = json.load(fp)
#                     if resource_id := data.get("id"):
#                         resource_hash = uri_hash(resource_id)
#                         if resource_hash not in filepath:
#                             resource_path = os.path.join(
#                                 os.path.dirname(filepath), resource_hash + ".json"
#                             )
#                             if verbose:
#                                 print(f"Renaming {filepath} to {resource_path}")
#                             shutil.move(filepath, resource_path)
#     except Exception as ex:
#         raise click.ClickException(ex.args[0])
