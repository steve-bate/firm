import glob
import hashlib
import json
import logging
from functools import lru_cache
from os import PathLike
from pathlib import Path
from typing import Any, cast

from firm.interfaces import JSONObject, QueryCriteria
from firm.store.base import ResourceStoreBase

log = logging.getLogger(__name__)


def uri_hash(uri: str) -> str:
    md5 = hashlib.md5()
    md5.update(uri.encode())
    return md5.hexdigest()


class FileResourceStore(ResourceStoreBase):  # type: ignore
    def __init__(self, store_path: PathLike[Any], partition_name: str = ""):
        self.store_path = (
            store_path if isinstance(store_path, Path) else Path(store_path)
        )
        self._partition_path = self.store_path / partition_name
        self._partition_path.mkdir(parents=True, exist_ok=True)
        log.info("FileResourceStore initialized at '%s'", self._partition_path)

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
