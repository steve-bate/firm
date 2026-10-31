"""Convert the configured store into another storage backend.

``--target-type`` names the destination backend and ``--target-dir`` the root it is written
under; the source is whatever the server configuration points at, so the same command converts
in either direction. Uploaded files in the tenant's ``files`` directory are not copied: only
documents and the items of collections.
"""

from dataclasses import replace
from pathlib import Path

import click

from firm.core.interfaces import ResourceStore
from firm.core.store.transfer import DestinationNotEmpty, copy_store
from firm.server.config import (
    FileStoreConfig,
    ServerConfig,
    SqliteStoreConfig,
    StorageKind,
)
from firm.server.server import init_tenant
from firm.server.utils import async_command

from . import Context, cli

StoreConfig = FileStoreConfig | SqliteStoreConfig


@cli.group()
def store() -> None:
    """Storage conversion"""


@store.command("convert")
@click.option(
    "--target-type",
    type=click.Choice([StorageKind.FILESYSTEM.value, StorageKind.SQLITE.value]),
    required=True,
    help="The kind of store to copy into.",
)
@click.option(
    "--target-dir",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Root directory for the destination store.",
)
@click.option("--force", is_flag=True, help="Overwrite a destination that already holds documents.")
@click.pass_obj
@async_command
async def convert(context: Context, target_type: str, target_dir: Path, force: bool) -> None:
    """Copy the configured store into a store of another kind."""
    await _convert_tenants(context, _target_store(target_type, target_dir), force)


def _target_store(target_type: str, target_dir: Path) -> StoreConfig:
    """The configuration of the store to copy into."""
    match StorageKind(target_type):
        case StorageKind.FILESYSTEM:
            return FileStoreConfig(base=target_dir)
        case StorageKind.SQLITE:
            return SqliteStoreConfig(base=target_dir)
    raise click.ClickException(  # unreachable: the option is a Choice over these two kinds
        f"{target_type} is not a kind of store a copy can be written to"
    )


async def _convert_tenants(context: Context, destination_store: StoreConfig, force: bool) -> None:
    """Copy public and private, for every tenant the command was asked about."""
    _refuse_the_same_store(context.config, destination_store)
    destination_config = replace(context.config, store=destination_store)
    for prefix in _prefixes(context):
        source = init_tenant(context.config, prefix)
        destination = init_tenant(destination_config, prefix)
        try:
            for name in ("public", "private"):
                await _copy_partition(
                    prefix,
                    name,
                    getattr(source, f"{name}_store"),
                    getattr(destination, f"{name}_store"),
                    force,
                )
        finally:
            for tenant in (source, destination):
                await tenant.public_store.close()
                await tenant.private_store.close()


async def _copy_partition(
    prefix: str, name: str, source: ResourceStore, destination: ResourceStore, force: bool
) -> None:
    try:
        report = await copy_store(source, destination, force=force)
    except DestinationNotEmpty as error:
        raise click.ClickException(f"{prefix} {name}: {error}") from None
    click.echo(
        f"{prefix} {name}: {report.documents} documents, "
        f"{report.collections} collections, {report.items} items"
    )


def _prefixes(context: Context) -> list[str]:
    """The tenants to convert: the one asked for on the command line, or all configured ones."""
    if context.tenant_uri:
        return [context.tenant_uri]
    return [tenant.prefix for tenant in context.config.tenants]


def _refuse_the_same_store(config: ServerConfig, destination: StoreConfig) -> None:
    if (
        config.store.kind is destination.kind
        and getattr(config.store, "base", None) == destination.base
    ):
        raise click.ClickException(
            "The destination is the configured store; choose a different --target-dir."
        )
