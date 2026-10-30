import time

import click

from firm.server.server import init_remote_cache
from firm.server.utils import async_command

from . import Context, cli


@cli.group()
def cache():
    """Remote document cache"""


@cache.command("purge")
@click.pass_obj
@async_command
async def purge_cache(ctx: Context) -> None:
    """Remove the entries whose TTL has expired, and report how many went.

    The cache is instance-wide, so this runs without a tenant.
    """
    remote_cache = init_remote_cache(ctx.config)
    try:
        removed = await remote_cache.purge_expired(time.time())
    finally:
        await remote_cache.close()
    click.echo(f"purged {removed} expired entries from {type(remote_cache).__name__}")
