import asyncio
import logging
import os
from dataclasses import dataclass, field

import click
import coloredlogs
import dotenv

from firm.core.interfaces import ResourceStore, Tenant
from firm.server.config import ServerConfig, load_config
from firm.server.server import init_tenant

dotenv.load_dotenv()

log = logging.root


@dataclass
class Context:
    tenant_uri: str
    config: ServerConfig

    _tenant: Tenant | None = field(default=None, init=False)

    def get_tenant(self) -> Tenant:
        if not self._tenant:
            self._tenant = init_tenant(self.config, self.tenant_uri)
        return self._tenant

    def get_store(self, uri: str) -> ResourceStore:
        """Get the appropriate store based on the URI."""
        tenant = self.get_tenant()
        return tenant.private_store if uri.startswith("urn:") else tenant.public_store


@click.group(context_settings=dict(auto_envvar_prefix="FIRM"))
@click.option(
    "--config",
    "config_file",
    type=click.File("r"),
    envvar="FIRM_CONFIG",
)
@click.option(
    "--tenant",
    envvar="FIRM_TENANT",
)
@click.pass_context
def cli(ctx: click.Context, config_file: click.File, tenant: str):
    """FIRM - Federated Information Resource Manager

    To get subcommand help, use '<subcommand> --help'
    """
    if tenant is None:
        raise click.UsageError(
            "Tenant URI is required. Use --tenant option " "or set FIRM_TENANT env variable."
        )
    coloredlogs.install()
    os.environ["FIRM_CONFIG"] = config_file.name
    config = load_config(config_file)
    ctx.obj = Context(tenant_uri=tenant, config=config)


@cli.result_callback()
@click.pass_obj
def after_command(ctx: Context, result, **kwargs):
    async def close_stores():
        await ctx.get_tenant().public_store.close()
        await ctx.get_tenant().private_store.close()

    asyncio.run(close_stores())
    log.info("tenant stores closed")


class LiteralChoice(click.ParamType):
    name = "literal"

    def __init__(self, literal):
        self.values = literal.__args__

    def convert(self, value, param, ctx):
        if value in self.values:
            return value
        self.fail(
            f'{value} is not a valid choice. Choose from {", ".join(self.values)}.',
            param,
            ctx,
        )
