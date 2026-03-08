import json
import uuid
from typing import Any
from urllib.parse import urlparse

import click

from firm.core.auth.keys import create_key_pair
from firm.core.interfaces import FIRM_NS, get_uri_prefix
from firm.server.utils import async_command

from . import Context, cli


@cli.group
def actor():
    """Actor management"""


def _property(p):
    if "=" in p:
        name, value = p.split("=")
    else:
        name = p
        value = p
    if value.startswith("http"):
        url = urlparse(value)
        value = (
            f'<a href="{value}" target="_blank" '
            'rel="nofollow noopener noreferrer me" translate="no">'
            f'<span class="invisible scheme">{url.scheme}://</span>'
            f'<span class="hostpath">{url.netloc}{url.path}</span></a>'
        )
    return {
        "type": "PropertyValue",
        "name": name,
        "value": value,
    }


@actor.command("create")
@click.argument("uri")
@click.argument("name")
@click.argument("handle")
@click.option("--role", "roles", multiple=True, default=[])
@click.option("--description")
@click.option("--header-image")
@click.option("--avatar")
@click.option("--hashtag", "hashtags", multiple=True)
@click.option("--property", "properties", multiple=True)
@click.pass_obj
@async_command
async def actor_create(
    ctx: Context,
    uri: str,
    name: str,
    handle: str,
    roles: list[str],
    description: str | None,
    header_image: str | None,
    avatar: str | None,
    hashtags: list[str],
    properties: list[str],
) -> None:
    """Create a new actor"""
    key_pair = create_key_pair()
    url = urlparse(uri)
    tenant_prefix = get_uri_prefix(uri)
    actor_resource: dict[str, Any] = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": uri,
        "preferredUsername": handle,
        "type": "Person",
        "url": uri,
        "name": name,
        "publicKey": {
            "id": f"{uri}#main-key",
            "owner": f"{uri}",
            "publicKeyPem": key_pair.public,
        },
        # "subtitle": "Europe's news in English",
        "inbox": f"{uri}/inbox",
        "outbox": f"{uri}/outbox",
        "followers": f"{uri}/followers",
        "alsoKnownAs": f"acct:{handle}@{url.hostname}",
    }
    if description:
        actor_resource["summary"] = description
    if header_image:
        actor_resource["image"] = header_image
    if avatar:
        actor_resource["icon"] = avatar
    if hashtags:
        actor_resource["tag"] = [
            {
                "type": "Hashtag",
                "href": f"{tenant_prefix}/tag/{h}",
                "name": f"#{h}",
            }
            for h in hashtags
        ]
    if properties:
        actor_resource["attachment"] = [_property(p) for p in properties]
    resources: list[dict] = [
        actor_resource,
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"{uri}/inbox",
            "attributedTo": uri,
            "type": "OrderedCollection",
            "totalItems": 0,
        },
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"{uri}/outbox",
            "attributedTo": uri,
            "type": "OrderedCollection",
            "totalItems": 0,
        },
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"{uri}/following",
            "attributedTo": uri,
            "type": "Collection",
            "totalItems": 0,
        },
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"{uri}/followers",
            "attributedTo": uri,
            "type": "Collection",
            "totalItems": 0,
        },
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"urn:uuid:{uuid.uuid4()}",
            "attributedTo": uri,
            "type": [FIRM_NS.Credentials.value],
            FIRM_NS.privateKey.value: key_pair.private,
            FIRM_NS.role.value: roles or None,
        },
    ]
    for r in resources:
        store = ctx.get_store(r["id"])
        if await store.is_stored(r["id"]):
            await store.remove(r["id"])
        await store.put(r)
        print(f"Wrote {r['id']}")


@actor.command("update")
@click.argument("uri")
@click.option("--name")
@click.option("--handle")
@click.option("--role", "roles", multiple=True, default=[])
@click.option("--description")
@click.option("--header-image")
@click.option("--avatar")
@click.option("--hashtag", "hashtags", multiple=True)
@click.option("--property", "properties", multiple=True)
@click.option("--add-property", "added_properties", multiple=True)
@click.option("--remove-property", "removed_properties", multiple=True)
@click.option("--verbose", "-v", is_flag=True)
@click.pass_obj
@async_command
async def actor_update(
    ctx: Context,
    uri: str,
    name: str | None,
    handle: str | None,
    roles: list[str],
    description: str | None,
    header_image: str | None,
    avatar: str | None,
    hashtags: list[str],
    properties: list[str],
    added_properties: list[str],
    removed_properties: list[str],
    verbose: bool,
) -> None:
    """Update actor properties"""
    actor_resource = await ctx.get_tenant().public_store.get(uri)
    credentials = None
    if not actor_resource:
        raise click.ClickException(f"Actor not found: {uri}")
    if name:
        actor_resource["name"] = name
    if handle:
        url = urlparse(uri)
        actor_resource["preferredUsername"] = handle
        actor_resource["alsoKnownAs"] = f"acct:{handle}@{url.hostname}"
    if roles:
        credentials = await ctx.get_tenant().private_store.query_one(
            {
                "type": FIRM_NS.Credentials.value,
                "attributedTo": uri,
            }
        )
        credentials[FIRM_NS.role.value] = roles
    if description:
        actor_resource["summary"] = description
    if header_image:
        actor_resource["image"] = {"type": "Image", "url": header_image}
    if avatar:
        actor_resource["icon"] = {"type": "Image", "url": avatar}
    if hashtags:
        tenant_prefix = get_uri_prefix(uri)
        actor_resource["tag"] = [
            {
                "type": "Hashtag",
                "href": f"{tenant_prefix}/tag/{h}",
                "name": f"#{h}",
            }
            for h in hashtags
        ]
    if properties:
        actor_resource["attachment"] = [_property(p) for p in list(properties)]
    if added_properties:
        actor_resource["attachment"] = actor_resource.get("attachment", []) + [
            _property(p) for p in added_properties
        ]
    if removed_properties:
        actor_resource["attachment"] = [
            p for p in actor_resource.get("attachment", []) if p["name"] not in removed_properties
        ]
    if credentials:
        if verbose:
            print(json.dumps(credentials, indent=2))
        await ctx.get_tenant().private_store.put(credentials)
    if verbose:
        print(json.dumps(actor_resource, indent=2))
    await ctx.get_tenant().public_store.put(actor_resource)


@actor.group
def outbox():
    """Outbox management"""


@outbox.command("clean")
@click.argument("uri")
@click.pass_obj
@async_command
async def actor_outbox_clean(ctx: Context, uri: str):
    store = ctx.get_tenant().public_store
    actor = await store.get(uri)
    box = await store.get(actor["outbox"])
    if isinstance(box, str):
        box = await store.get(box)
    if activity_uris := box.get("orderedItems", []):
        for activity_uri in activity_uris:
            if activity := await store.get(activity_uri):
                obj = activity.get("object")
                if isinstance(obj, str):
                    obj = await store.get(obj)
                if obj.get("attributedTo") == actor["id"]:
                    await store.remove(obj["id"])
                    print(f"removed object {obj}")
                await store.remove(activity_uri)
                print(f"removed activity {activity_uri}")
        box.pop("orderedItems")
        await store.put(box)


@actor.group
def inbox():
    """Inbox management"""


@inbox.command("clean")
@click.argument("uri")
@click.pass_obj
@async_command
async def actor_inbox_clean(ctx: Context, uri: str):
    store = ctx.get_tenant().public_store
    actor = await store.get(uri)
    box = await store.get(actor["inbox"])
    box.pop("orderedItems")
    await store.put(box)
