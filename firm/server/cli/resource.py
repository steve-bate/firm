import json
from typing import IO

import click

from firm.server.utils import async_command

from . import Context, cli


@cli.group
def resource():
    """Resource management"""


@resource.command("add")
@click.argument("file", type=click.File("r"))
@click.pass_obj
@async_command
async def add_resource(ctx: Context, file: IO) -> None:
    """Add a resource from a file."""
    resource = json.loads(file.read())
    if "id" not in resource:
        raise click.ClickException("Resource missing 'id' field")
    await ctx.get_store(resource["id"]).put(resource)


@resource.command("remove")
@click.argument("uri")
@click.pass_obj
@async_command
async def remove_resource(ctx: Context, uri: str) -> None:
    """Remove a resource"""
    await ctx.get_store(uri).remove(uri)


@resource.command("get")
@click.argument("uri")
@click.pass_obj
@async_command
async def get_resource(ctx: Context, uri: str) -> None:
    """Get a resource"""
    resource = ctx.get_store(uri).get(uri)
    print(json.dumps(resource, indent=2))


@resource.command("query")
@click.pass_obj
@click.argument("criteria")
@async_command
async def resource_query(ctx: Context, criteria: str) -> None:
    """Query resources"""
    query = json.loads(criteria)
    public_resources = await ctx.get_tenant().public_store.query(query)
    private_resources = await ctx.get_tenant().private_store.query(query)
    print(
        json.dumps(
            [
                {
                    "public": public_resources,
                    "private": private_resources,
                }
            ]
            + private_resources,
            indent=2,
        )
    )


if __name__ == "__main__":
    resource()
