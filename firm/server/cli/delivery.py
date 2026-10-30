"""Monitor and manage the delivery queue from the command line."""

import time

import click

from firm.core.delivery.queue import DeliveryQueue, DeliveryState
from firm.core.resolver import ResourceResolver
from firm.server.adapters import HttpxTransport
from firm.server.delivery import FirmDeliveryService
from firm.server.server import (
    init_delivery_queue,
    init_remote_provider,
    init_remote_signer,
    init_tenant,
)
from firm.server.utils import async_command

from . import Context, cli


@cli.group()
def delivery():
    """Outbound deliveries"""


@delivery.command("list")
@click.option("--state", type=click.Choice([state.value for state in DeliveryState]), default=None)
@click.option("--limit", default=50, show_default=True)
@click.pass_obj
@async_command
async def list_deliveries(ctx: Context, state: str | None, limit: int) -> None:
    """Print the units of work the queue is holding.

    The queue is instance-wide, so this runs without a tenant.
    """
    queue = await _open(ctx)
    try:
        rows = await queue.list(
            state=DeliveryState(state) if state is not None else None, limit=limit
        )
    finally:
        await queue.close()
    for row in rows:
        click.echo(
            f"{row.id} {row.state:<9} attempts={row.attempts}"
            f" next={row.next_attempt_at:.0f} {row.endpoint}"
        )
    if not rows:
        click.echo(f"no deliveries in {type(queue).__name__}")


@delivery.command("retry")
@click.argument("delivery_id")
@click.pass_obj
@async_command
async def retry_delivery(ctx: Context, delivery_id: str) -> None:
    """Attempt one delivery now, whatever its state."""
    queue = await _open(ctx)
    try:
        row = await queue.get(delivery_id)
        if row is None:
            raise click.ClickException(f"no delivery {delivery_id}")
        if not ctx.config.is_local(row.endpoint) and _signing_actor(ctx) is None:
            raise click.ClickException(
                "a remote delivery must be signed: set remote_fetch.signing_actor"
            )
        service, resolver = await _service(ctx, queue)
        try:
            await queue.reschedule(delivery_id, next_attempt_at=time.time(), reason="retry")
            due = await queue.get(delivery_id)
            if due is not None:
                await service.run(due)
            after = await queue.get(delivery_id)
        finally:
            await resolver.close()
    finally:
        await queue.close()
    if after is None:
        click.echo(f"{delivery_id} is gone")
        return
    click.echo(
        f"{after.id} {after.state} attempts={after.attempts} {after.last_reason or ''}".rstrip()
    )


@delivery.command("cancel")
@click.argument("delivery_id")
@click.pass_obj
@async_command
async def cancel_delivery(ctx: Context, delivery_id: str) -> None:
    """Abandon a delivery; it will not be retried."""
    queue = await _open(ctx)
    try:
        row = await queue.get(delivery_id)
        if row is None:
            raise click.ClickException(f"no delivery {delivery_id}")
        await queue.abandon(delivery_id, reason="cancelled")
    finally:
        await queue.close()
    click.echo(f"{delivery_id} cancelled")


@delivery.command("purge")
@click.option("--all", "purge_all", is_flag=True, help="Also drop abandoned rows")
@click.pass_obj
@async_command
async def purge_deliveries(ctx: Context, purge_all: bool) -> None:
    """Drop delivered history, and abandoned rows with --all."""
    queue = await _open(ctx)
    try:
        # TTL 0: the operator asked for the history, not for a TTL sweep.
        removed = await queue.prune(now=time.time(), history_ttl=0.0, abandoned=purge_all)
    finally:
        await queue.close()
    click.echo(f"purged {removed} rows from {type(queue).__name__}")


async def _open(ctx: Context) -> DeliveryQueue:
    return init_delivery_queue(ctx.config)


def _signing_actor(ctx: Context) -> str | None:
    return ctx.config.remote_fetch.signing_actor


async def _service(
    ctx: Context, queue: DeliveryQueue
) -> tuple[FirmDeliveryService, ResourceResolver]:
    """The service the server would build, and the registry it needs to find tenants."""
    tenants = {
        tenant.prefix: init_tenant(ctx.config, tenant.prefix) for tenant in ctx.config.tenants
    }
    provider = init_remote_provider(ctx.config)
    resolver = ResourceResolver(tenants, provider)
    signer = await init_remote_signer(ctx.config, resolver)
    if provider is not None and signer is not None:
        provider.set_signer(signer)
    service = FirmDeliveryService(ctx.config, HttpxTransport(), queue=queue)
    service.set_resolver(resolver)
    return service, resolver
