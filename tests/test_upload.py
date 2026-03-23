import pytest

from tests.conftest import TENANT_PREFIX


def _iter_post_upload_paths(client) -> list[str]:
    paths: list[str] = []

    app = getattr(client, "app", None)
    routes = getattr(app, "routes", []) if app is not None else []
    for route in routes:
        path = getattr(route, "path", None)
        methods = {m.upper() for m in getattr(route, "methods", set())}
        if path and "POST" in methods and "upload" in path.lower():
            paths.append(path)

    return sorted(set(paths))


@pytest.fixture
def upload_path(client) -> str:
    paths = _iter_post_upload_paths(client)
    assert paths, "No POST upload endpoint found."
    return "/upload" if "/upload" in paths else paths[0]


def test_upload_endpoint_is_reachable(client, upload_path):
    resp = client.post(upload_path, files={})
    assert resp.status_code not in {404, 405, 500}


def upload_media(client, upload_endpoint, media_bytes, media_type):
    return client.post(
        upload_endpoint,
        data={"object": '{"type": "Image", "mediaType": "image/png"}'},
        files={"file": ("tiny.png", media_bytes, media_type)},
        headers={
            "Authorization": "Bearer abcd",
        },
    )


async def test_upload_image(client, actor, upload_path):
    tenant = client.app.state.tenants[TENANT_PREFIX]
    await tenant.private_store.put(
        {
            "id": "urn:uuid:529f85ec-1d5d-4513-bcc0-e0a33702a4b1",
            "attributedTo": actor["id"],
            "type": ["firm:Credentials"],
            "firm:token": "abcd",
        }
    )
    # Minimal 1x1 PNG
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\x0bIDATx\x9cc`\x00\x00\x00\x02\x00\x01"
        b"\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    resp = upload_media(client, upload_path, png_bytes, "image/png")

    assert resp.status_code < 500
    assert resp.status_code in {200, 201, 202, 204}
    assert resp.headers.get("Location") is not None
    location = resp.headers["Location"]
    assert location.startswith("http")
    resource = await tenant.public_store.get(location)
    assert resource is not None
    assert resource["type"] == "Image"
    assert resource["mediaType"] == "image/png"
    assert resource["attributedTo"] == actor["id"]
    assert resource["url"].endswith("png")
