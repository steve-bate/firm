import asyncio
import logging
import ssl

import click
import coloredlogs
import uvicorn

from firm.server.exceptions import ServerException
from firm.server.server import run

from . import Context, LiteralChoice, cli


@cli.command
@click.option("--host", metavar="HOST", default="0.0.0.0", show_default=True)
@click.option("--port", metavar="PORT", type=int, default="7000", show_default=True)
@click.option(
    "--verbose",
    "-v",
    type=bool,
    is_flag=True,
    default=False,
    show_default=True,
)
@click.option(
    "--loop",
    type=LiteralChoice(uvicorn.config.LoopSetupType),
    default="auto",
    show_default=True,
)
@click.option(
    "--http",
    type=LiteralChoice(uvicorn.config.HTTPProtocolType),
    default="auto",
    show_default=True,
)
@click.option("--ws-max-size", type=int, default=(16 * 1024 * 1024), show_default=True)
@click.option("--ws-max-queue", type=int, default=32, show_default=True)
@click.option("--ws-ping-interval", type=float, default=20.0, show_default=True)
@click.option("--ws-ping-timeout", type=float, default=20.0, show_default=True)
@click.option(
    "--ws-per-message-deflate",
    type=bool,
    is_flag=True,
    default=True,
    show_default=True,
)
@click.option(
    "--interface",
    type=LiteralChoice(uvicorn.config.InterfaceType),
    default="auto",
    show_default=True,
)
@click.option(
    "--proxy-headers",
    type=bool,
    is_flag=True,
    default=True,
    show_default=True,
)
@click.option(
    "--server-header",
    type=bool,
    is_flag=True,
    default=True,
    show_default=True,
)
@click.option("--date-header", type=bool, is_flag=True, default=True, show_default=True)
@click.option("--limit-concurrency", type=int, default=None, show_default=True)
@click.option("--limit-max-requests", type=int, default=None, show_default=True)
@click.option("--backlog", type=int, default=2048, show_default=True)
@click.option("--timeout-keep-alive", type=int, default=5, show_default=True)
@click.option("--timeout-notify", type=int, default=30, show_default=True)
@click.option("--timeout-graceful-shutdown", type=int, default=None, show_default=True)
@click.option("--ssl-keyfile", type=str, default=None, show_default=True)
@click.option("--ssl-certfile", type=str, default=None, show_default=True)
@click.option("--ssl-keyfile-password", type=str, default=None, show_default=True)
@click.option(
    "--ssl-version",
    type=int,
    default=uvicorn.config.SSL_PROTOCOL_VERSION,
    show_default=True,
)
@click.option("--ssl-cert-reqs", type=int, default=ssl.CERT_NONE, show_default=True)
@click.option("--ssl-ca-certs", type=str, default=None, show_default=True)
@click.option("--ssl-ciphers", type=str, default="TLSv1", show_default=True)
@click.option(
    "--h11-max-incomplete-event-size",
    type=int,
    default=None,
    show_default=True,
)
@click.pass_obj
def serve(ctx: Context, verbose: bool, **kwargs):
    """Run the server"""
    try:
        if verbose:
            coloredlogs.set_level(logging.DEBUG)
        run(ctx.config, verbose, kwargs)
    except (asyncio.exceptions.CancelledError, KeyboardInterrupt):
        pass
    except ServerException as ex:
        logging.log(ex.level, ex.message)
        raise SystemExit(1)
