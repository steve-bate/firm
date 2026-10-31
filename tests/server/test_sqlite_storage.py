"""A tenant whose documents live in SQLite: the config, the wiring, and a collection.

The store itself is exercised by the contract test in `tests/core/store`; what is tested here
is that a `sqlite` configuration actually builds one, and that the wired tenant answers the
five collection methods from the table rather than from a document array.
"""

import pathlib

import pytest

from firm.core.store.sqlite import SqliteResourceStore
from firm.server.config import FileStoreConfig, ServerConfig, StorageKind, load_config
from firm.server.server import init_tenant_stores

PREFIX = "https://server.test"
COLLECTION = f"{PREFIX}/user1/outbox"


def _config(tmp_path: pathlib.Path, kind: str) -> ServerConfig:
    """A configuration file of the given storage kind, parsed the way the server parses it."""
    path = tmp_path / "config.yaml"
    path.write_text(f"tenants:\n  - prefix: {PREFIX}\nstore:\n  kind: {kind}\n  base: {tmp_path}\n")
    return load_config(str(path))


@pytest.fixture
def sqlite_config(tmp_path) -> ServerConfig:
    return _config(tmp_path, "sqlite")


def test_the_loader_accepts_a_sqlite_store(tmp_path):
    assert _config(tmp_path, "sqlite").store.kind is StorageKind.SQLITE


def test_the_loader_still_reads_a_filesystem_store(tmp_path):
    config = _config(tmp_path, "filesystem")

    assert config.store.kind is StorageKind.FILESYSTEM
    assert isinstance(config.store, FileStoreConfig)


async def test_a_sqlite_tenant_gets_a_store_per_partition(tmp_path, sqlite_config):
    public, private, files = init_tenant_stores(sqlite_config, PREFIX)

    assert isinstance(public, SqliteResourceStore) and isinstance(private, SqliteResourceStore)
    assert public is not private, "partitions are separate stores"
    await public.put({"id": f"{PREFIX}/note/1", "type": "Note"})
    stored = await public.get(f"{PREFIX}/note/1")
    assert stored is not None and stored["type"] == "Note"
    assert await private.get(f"{PREFIX}/note/1") is None, "a partition sees only its own rows"
    tenant_dir = tmp_path / "tenants" / "server.test"
    assert files.parent == tenant_dir
    assert (tenant_dir / "public.sqlite").exists()
    assert (tenant_dir / "private.sqlite").exists()


async def test_a_sqlite_tenant_serves_a_collection_from_the_table(sqlite_config):
    public, _private, _files = init_tenant_stores(sqlite_config, PREFIX)
    await public.put({"id": COLLECTION, "type": "OrderedCollection", "items": ["a", "b", "c"]})

    page = await public.page_items(COLLECTION, limit=2)

    assert page.items == ("a", "b"), "newest first, imported from the document"
    assert page.total == 3
    assert page.cursor is not None
    rest = await public.page_items(COLLECTION, page.cursor, limit=2)
    assert rest.items == ("c",)
    assert rest.cursor is None
