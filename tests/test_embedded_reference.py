"""A peer-shaped Create must not damage the documents it only names.

The reported bug, end to end: PeerTube's published ``Video`` example writes
``attributedTo`` as an embedded ``{type: Group, id}`` node, and FIRM used to store that
node over the channel actor — leaving a channel with no outbox, no followers and no
owning account.
"""

AS2 = "https://www.w3.org/ns/activitystreams"
PUBLIC = f"{AS2}#Public"
HEADERS = {"content-type": "application/activity+json", "accept": "application/activity+json"}


async def _channel(tenant, actor, channel: str) -> None:
    await tenant.public_store.put(
        {
            "id": channel,
            "type": "Group",
            "preferredUsername": f"{channel.rsplit('/', 1)[-1]}",
            "attributedTo": [{"type": "Person", "id": actor["id"]}],
            "outbox": f"{channel}/outbox",
            "followers": f"{channel}/followers",
        }
    )
    for box in (f"{channel}/outbox", f"{channel}/followers"):
        await tenant.public_store.put(
            {"id": box, "type": "OrderedCollection", "attributedTo": channel, "items": []}
        )


async def _outbox(tenant, actor) -> str:
    outbox = f"{actor['id']}/outbox"
    actor["outbox"] = outbox
    await tenant.public_store.put(
        {"id": outbox, "type": "OrderedCollection", "attributedTo": actor["id"], "items": []}
    )
    return outbox


def _create_video(actor_uri: str, channel: str, attributed_to) -> dict:
    return {
        "@context": [
            AS2,
            {"pt": "https://joinpeertube.org/ns#", "sc": "http://schema.org/"},
        ],
        "type": "Create",
        "actor": actor_uri,
        "to": [PUBLIC, channel],
        "cc": [f"{channel}/followers", f"{actor_uri}/followers"],
        "object": {
            "type": "Video",
            "name": "A walk through the market",
            "content": "Ten minutes in Les Halles.",
            "mediaType": "text/markdown",
            "uuid": "04af977f-4201-4697-be67-a8d8cae6fa7a",
            "duration": "PT600S",
            "attributedTo": attributed_to,
            "url": [
                {
                    "type": "Link",
                    "mediaType": "video/mp4",
                    "href": "http://patch.test/media/abc.mp4",
                }
            ],
        },
    }


async def test_an_embedded_channel_node_does_not_replace_the_channel(client, actor):
    tenant = next(iter(client.app.state.tenants.values()))
    actor_uri = actor["id"]
    channel = f"{actor_uri}_channel"
    await _channel(tenant, actor, channel)
    outbox = await _outbox(tenant, actor)

    response = client.post(
        outbox,
        json=_create_video(actor_uri, channel, [{"type": "Group", "id": channel}]),
        headers=HEADERS,
    )
    assert response.status_code == 201

    stored_channel = await tenant.public_store.get(channel)
    assert stored_channel is not None
    assert stored_channel["outbox"] == f"{channel}/outbox", "the channel kept its outbox"
    assert stored_channel["followers"] == f"{channel}/followers"
    assert stored_channel["attributedTo"] == [{"type": "Person", "id": actor_uri}]

    activity = await tenant.public_store.get(response.headers["location"])
    video = await tenant.public_store.get(activity["object"])
    assert video is not None
    assert video["attributedTo"] == [channel], "the video keeps the channel as a reference"
    assert video["uuid"] == "04af977f-4201-4697-be67-a8d8cae6fa7a", "nothing else was lost"


async def test_the_iri_form_stores_the_same_reference(client, actor):
    tenant = next(iter(client.app.state.tenants.values()))
    actor_uri = actor["id"]
    channel = f"{actor_uri}_channel"
    await _channel(tenant, actor, channel)
    outbox = await _outbox(tenant, actor)

    response = client.post(
        outbox, json=_create_video(actor_uri, channel, [channel]), headers=HEADERS
    )
    assert response.status_code == 201

    activity = await tenant.public_store.get(response.headers["location"])
    video = await tenant.public_store.get(activity["object"])
    assert video is not None and video["attributedTo"] == [channel]


async def test_the_embedded_form_does_not_create_a_document_that_is_not_the_posters(
    client,
    actor,
):
    """A node naming another actor's namespace leaves a reference, and stores nothing."""
    tenant = next(iter(client.app.state.tenants.values()))
    actor_uri = actor["id"]
    channel = f"{actor_uri}_channel"
    await _channel(tenant, actor, channel)
    outbox = await _outbox(tenant, actor)
    squatted = "http://patch.test/actors/someone_else"

    response = client.post(
        outbox,
        json=_create_video(actor_uri, channel, [{"type": "Group", "id": squatted}]),
        headers=HEADERS,
    )
    assert response.status_code == 201
    assert await tenant.public_store.get(squatted) is None
