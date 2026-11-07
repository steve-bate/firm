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


def test_an_upload_over_the_configured_cap_is_rejected(tmp_path) -> None:
    """A cap the server owns: a client cannot be asked to guess a policy."""
    from fastapi import Request
    from fastapi.testclient import TestClient

    from firm.core.interfaces import Principal
    from firm.server.auth import get_principal
    from firm.server.config import (
        MediaConfig,
        MemoryStoreConfig,
        ServerConfig,
        TenantConfig,
    )
    from firm.server.server import app_factory, clear_app

    clear_app()
    config = ServerConfig(
        [TenantConfig(prefix="http://cap.test")],
        MemoryStoreConfig(tmp_path),
        media=MediaConfig(max_upload_bytes=1024),
    )
    app = app_factory(config)

    async def _principal(request: Request) -> Principal:
        actor = {"id": "http://cap.test/actors/ada", "type": "Person"}
        return Principal(actor, request.state.tenant)  # type: ignore[arg-type]

    app.dependency_overrides[get_principal] = _principal
    try:
        with TestClient(app, base_url="http://cap.test") as client:
            response = client.post(
                "/media/upload",
                files={
                    "object": (None, json.dumps({"type": "Image"})),
                    "file": ("big.bin", b"x" * 2048, "application/octet-stream"),
                },
            )
    finally:
        clear_app()
    assert response.status_code == 413, response.text
    assert "1024" in response.text


def test_the_upload_is_streamed_to_disk_not_read_into_one_bytes_object(
    client: TestClient, actor, monkeypatch
) -> None:
    """A 4 MiB upload must be copied in chunks; reading it whole is what OOMs on video."""
    from firm.server import media as media_module

    calls: list[int] = []
    original = media_module._copy_stream_to_file

    def spy(source, destination, *, chunk_size=media_module._COPY_CHUNK, max_bytes=None):
        calls.append(chunk_size)
        return original(source, destination, chunk_size=chunk_size, max_bytes=max_bytes)

    monkeypatch.setattr(media_module, "_copy_stream_to_file", spy)
    response = client.post(
        "/media/upload",
        files={
            "object": (None, json.dumps({"type": "Video"})),
            "file": ("clip.mp4", b"\0" * (4 * 1024 * 1024), "video/mp4"),
        },
    )
    assert response.status_code == 201, response.text
    assert calls, "the stream copy helper was never called"


def test_media_serves_byte_ranges_so_a_video_can_seek(client: TestClient, actor) -> None:
    payload = b"0123456789" * 100
    body = client.post(
        "/media/upload",
        files={
            "object": (None, json.dumps({"type": "Video"})),
            "file": ("clip.mp4", payload, "video/mp4"),
        },
    ).json()
    response = client.get(body["url"], headers={"Range": "bytes=100-199"})
    assert response.status_code == 206, response.text
    assert response.headers["content-range"] == f"bytes 100-199/{len(payload)}"
    assert response.content == payload[100:200]
    assert response.headers["accept-ranges"] == "bytes"


def test_head_answers_with_the_same_metadata_and_no_body(client: TestClient, actor) -> None:
    """Peers probe with HEAD before downloading; a 404 there discards the file.

    Pixelfed's avatar fetcher asks for the mime type and length first and skips
    the download entirely when the probe fails, so the media route has to answer
    HEAD as well as GET.
    """
    payload = b"png" * 64
    body = client.post(
        "/media/upload",
        files={
            "object": (None, json.dumps({"type": "Image", "to": [AP_PUBLIC]})),
            "file": ("photo.png", payload, "image/png"),
        },
    ).json()

    response = client.head(body["url"])

    assert response.status_code == 200, response.text
    assert response.content == b"", "HEAD must not carry a body"
    assert response.headers["content-length"] == str(len(payload))
    assert response.headers["content-type"] == "image/png"


def test_head_of_a_missing_file_is_404(client: TestClient, actor) -> None:
    response = client.head(f"/actors/{actor}/media/does-not-exist.png")
    assert response.status_code == 404
