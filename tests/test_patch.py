from urllib.parse import urlparse

import pytest
from httpx import HTTPStatusError

AS2_CONTEXT = "https://www.w3.org/ns/activitystreams"
PUBLIC = "https://www.w3.org/ns/activitystreams#Public"


def do_patch(client, actor, operations):
    outbox = actor["outbox"]

    create_activity = {
        "@context": AS2_CONTEXT,
        "type": "Create",
        "actor": actor["id"],
        "object": {
            "type": "Note",
            "attributedTo": actor["id"],
            "summary": "Test Note",
            "content": "before patch",
        },
    }

    create_response = client.post(
        outbox,
        json=create_activity,
        headers={"Content-Type": "application/activity+json"},
    )
    create_response.raise_for_status()

    object_id = create_response.headers["Location"]
    assert object_id is not None

    PATCH_CONTEXT = {
        "@context": [
            {
                "@version": 1.1,
                "fep-a1d1": "https://w3id.org/fep/a1d1#",
                "Patch": "fep-a1d1:Patch",
                "PatchOperations": {
                    "@id": "fep-a1d1:PatchOperations",
                    "@context": {
                        "operations": {
                            "@id": "fep-a1d1:operations",
                            "@container": "@list",
                            "@context": {
                                "op": "fep-a1d1:op",
                                "path": "fep-a1d1:path",
                                "value": {"@id": "fep-a1d1:value", "@type": "@json"},
                                "from": "fep-a1d1:from",
                                "add": "fep-a1d1:add",
                                "remove": "fep-a1d1:remove",
                                "replace": "fep-a1d1:replace",
                                "move": "fep-a1d1:move",
                                "copy": "fep-a1d1:copy",
                                "test": "fep-a1d1:test",
                            },
                        }
                    },
                },
            }
        ]
    }

    patch_activity = {
        "@context": [PATCH_CONTEXT, AS2_CONTEXT],
        "type": "Patch",
        "actor": actor["id"],
        "object": {
            "type": "PatchOperations",
            "operations": operations,
        },
        "target": object_id,
    }

    patch_response = client.post(
        outbox,
        json=patch_activity,
        headers={"Content-Type": "application/activity+json"},
    )
    patch_response.raise_for_status()

    object_response = client.get(
        urlparse(object_id).path, headers={"Accept": "application/activity+json"}
    )
    assert object_response.status_code == 200

    return object_response.json()


def test_patch_replace(client, actor):
    patched_object = do_patch(
        client, actor, [{"op": "replace", "path": "/content", "value": "after patch"}]
    )
    assert patched_object["content"] == "after patch"


def test_patch_add(client, actor):
    patched_object = do_patch(client, actor, [{"op": "replace", "path": "/foo", "value": "bar"}])
    assert patched_object["foo"] == "bar"


def test_patch_remove(client, actor):
    patched_object = do_patch(client, actor, [{"op": "remove", "path": "/object/summary"}])
    assert "summary" not in patched_object["object"]


def test_patch_move(client, actor):
    patched_object = do_patch(
        client, actor, [{"op": "move", "from": "/object/summary", "path": "/object/name"}]
    )
    assert "summary" not in patched_object["object"]
    assert "name" in patched_object["object"]
    assert patched_object["object"]["name"] == "Test Note"


def test_patch_copy(client, actor):
    patched_object = do_patch(
        client, actor, [{"op": "copy", "from": "/object/summary", "path": "/object/name"}]
    )
    assert "summary" in patched_object["object"]
    assert patched_object["object"]["summary"] == "Test Note"
    assert "name" in patched_object["object"]
    assert patched_object["object"]["name"] == "Test Note"


def test_patch_test(client, actor):
    do_patch(client, actor, [{"op": "test", "path": "/object/summary", "value": "Test Note"}])


def test_patch_test_fail(client, actor):
    with pytest.raises(HTTPStatusError):
        do_patch(client, actor, [{"op": "test", "path": "/object/summary", "value": "Wrong Value"}])
