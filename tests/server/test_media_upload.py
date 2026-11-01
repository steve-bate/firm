"""The media upload endpoint over HTTP: the id names an object, the url names a file.

The service tests cover the decisions; these cover the two lookups that make an
upload useful — fetching the AS2 object by its id and fetching the bytes by its
url. Nothing exercised either route before, so an id in the file route's URL
space went unnoticed.
"""

import hashlib
import json

from fastapi.testclient import TestClient

AP_PUBLIC = "https://www.w3.org/ns/activitystreams#Public"


def _upload(client: TestClient, object_shell: dict | None = None):
    shell = object_shell if object_shell is not None else {"type": "Image", "to": [AP_PUBLIC]}
    return client.post(
        "/media/upload",
        files={
            "object": (None, json.dumps(shell)),
            "file": ("photo.png", b"png", "image/png"),
        },
    )


def test_upload_returns_the_created_object_and_its_location(client: TestClient, actor) -> None:
    response = _upload(client)

    assert response.status_code == 201, response.text
    body = response.json()
    assert response.headers["Location"] == body["id"]
    assert body["type"] == "Image", "the client's shell survives"
    assert body["url"].endswith(".png"), "the url names the stored file"
    assert body["id"] != body["url"], "an object is not its file"


def test_the_id_fetches_the_object_and_the_url_fetches_the_bytes(client: TestClient, actor) -> None:
    body = _upload(client).json()

    object_response = client.get(body["id"], headers={"Accept": "application/activity+json"})
    assert object_response.status_code == 200, object_response.text
    assert object_response.json()["id"] == body["id"]
    assert object_response.json()["type"] == "Image"

    file_response = client.get(body["url"])
    assert file_response.status_code == 200, file_response.text
    assert file_response.content == b"png"


def test_a_create_wrapper_is_unwrapped_before_the_object_is_stored(
    client: TestClient, actor
) -> None:
    body = _upload(
        client,
        {"type": "Create", "object": {"type": "Image", "to": [AP_PUBLIC], "name": "shot"}},
    ).json()

    assert body["type"] == "Image", "the wrapper is not what gets stored"
    assert body["name"] == "shot"
    assert body["id"].startswith(f"{body['attributedTo']}/image/"), "id is in the actor's namespace"


def test_the_file_is_content_addressed(client: TestClient, actor) -> None:
    body = _upload(client).json()

    digest = hashlib.sha256(b"png").hexdigest()
    assert body["url"].endswith(f"/media/{digest}.png")
