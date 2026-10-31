"""`firm store convert` in both directions."""

import asyncio

import pytest
from click.testing import CliRunner

import firm.server.cli.main  # noqa: F401  (registers the subcommands)
from firm.core.store.file import FileResourceStore
from firm.core.store.sqlite import SqliteResourceStore
from firm.server.cli import cli as firm_cli

PREFIX = "https://server.test"
NOTE = f"{PREFIX}/note/1"
COLLECTION = f"{PREFIX}/user1/outbox"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """Forget the tenant the developer's shell may have selected.

    The `cli` group reads ``FIRM_CONFIG`` and ``FIRM_TENANT`` from the environment, and this
    repository's `.envrc.local` exports both, so a run through direnv or pre-commit would
    otherwise convert the tenant named there instead of the one in the test's config file.
    """
    monkeypatch.delenv("FIRM_CONFIG", raising=False)
    monkeypatch.delenv("FIRM_TENANT", raising=False)


def _config(tmp_path, kind: str, base) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(f"tenants:\n  - prefix: {PREFIX}\nstore:\n  kind: {kind}\n  base: {base}\n")
    return str(path)


def _to(target_type: str, directory) -> list[str]:
    return [
        "store",
        "convert",
        "--target-type",
        target_type,
        "--target-dir",
        str(directory),
    ]


def _seed_filesystem(base) -> None:
    async def seed() -> None:
        store = FileResourceStore(base / "tenants" / "server.test" / "public")
        await store.put({"id": NOTE, "type": "Note", "content": "hello"})
        await store.put(
            {
                "id": COLLECTION,
                "type": "OrderedCollection",
                "items": ["b", "a"],
                "totalItems": 2,
            }
        )

    asyncio.run(seed())


def _seed_sqlite(base) -> None:
    async def seed() -> None:
        directory = base / "tenants" / "server.test"
        directory.mkdir(parents=True, exist_ok=True)
        store = SqliteResourceStore("public", str(directory / "public.sqlite"))
        await store.put({"id": NOTE, "type": "Note", "content": "hello"})
        await store.put(
            {
                "id": COLLECTION,
                "type": "OrderedCollection",
                "items": ["b", "a"],
                "totalItems": 2,
            }
        )
        await store.close()

    asyncio.run(seed())


def _run(arguments: list[str]):
    return CliRunner().invoke(firm_cli, arguments)


def test_convert_writes_the_sqlite_store(tmp_path):
    source = tmp_path / "files"
    _seed_filesystem(source)
    config = _config(tmp_path, "filesystem", source)

    result = _run(["--config", config, *_to("sqlite", tmp_path / "db")])

    assert result.exit_code == 0, result.output
    assert "2 documents" in result.output
    assert "1 collections" in result.output
    assert "2 items" in result.output
    destination = SqliteResourceStore(
        "public", str(tmp_path / "db" / "tenants" / "server.test" / "public.sqlite")
    )
    page = asyncio.run(destination.page_items(COLLECTION, limit=10))
    note = asyncio.run(destination.get(NOTE))
    asyncio.run(destination.close())
    assert page.items == ("b", "a")
    assert note is not None and note["content"] == "hello"


def test_convert_writes_the_filesystem_store(tmp_path):
    database = tmp_path / "db"
    _seed_sqlite(database)
    config = _config(tmp_path, "sqlite", database)

    result = _run(["--config", config, *_to("filesystem", tmp_path / "files")])

    assert result.exit_code == 0, result.output
    destination = FileResourceStore(tmp_path / "files" / "tenants" / "server.test" / "public")
    page = asyncio.run(destination.page_items(COLLECTION, limit=10))
    note = asyncio.run(destination.get(NOTE))
    assert page.items == ("b", "a")
    assert note is not None and note["content"] == "hello"


def test_an_unsupported_target_type_is_refused(tmp_path):
    source = tmp_path / "files"
    _seed_filesystem(source)
    config = _config(tmp_path, "filesystem", source)

    result = _run(["--config", config, *_to("memory", tmp_path / "db")])

    assert result.exit_code == 2, result.output
    assert "--target-type" in result.output


def test_a_second_run_needs_force(tmp_path):
    source = tmp_path / "files"
    _seed_filesystem(source)
    config = _config(tmp_path, "filesystem", source)
    arguments = ["--config", config, *_to("sqlite", tmp_path / "db")]
    assert _run(arguments).exit_code == 0

    again = _run(arguments)
    forced = _run(arguments + ["--force"])

    assert again.exit_code == 1, again.output
    assert "already holds documents" in again.output
    assert forced.exit_code == 0, forced.output


def test_it_refuses_to_copy_the_store_onto_itself(tmp_path):
    source = tmp_path / "files"
    _seed_filesystem(source)
    config = _config(tmp_path, "filesystem", source)

    result = _run(["--config", config, *_to("filesystem", source)])

    assert result.exit_code == 1, result.output
    assert "the destination is the configured store" in result.output.lower()


def test_every_configured_tenant_is_converted(tmp_path):
    source = tmp_path / "files"
    _seed_filesystem(source)
    path = tmp_path / "config.yaml"
    path.write_text(
        "tenants:\n"
        f"  - prefix: {PREFIX}\n"
        "  - prefix: https://other.test\n"
        f"store:\n  kind: filesystem\n  base: {source}\n"
    )

    result = _run(["--config", str(path), *_to("sqlite", tmp_path / "db")])

    assert result.exit_code == 0, result.output
    assert result.output.count("public:") == 2, "one line per tenant's public store"
