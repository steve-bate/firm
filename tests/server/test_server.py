import os

import pytest
from pytest_httpx import HTTPXMock
from starlette.testclient import TestClient

from firm.core.auth.http_signature import HttpSignatureAuth
from firm.server.adapters import HttpxTransport
from firm.server.config import FileStoreConfig, ServerConfig
from firm.server.server import app_factory, clear_app

TENANT_PREFIX = "https://server.test"


@pytest.fixture
def client(tmp_path):
    prefix = TENANT_PREFIX
    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    with TestClient(
        app_factory(ServerConfig([prefix], FileStoreConfig(base=tmp_path))),
        base_url=prefix,
    ) as client:
        yield client
        clear_app()


PRIVATE_KEY = """-----BEGIN PRIVATE KEY-----
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
sirZG/SM1YwT2G4YpPGy06Z0Fo3J
-----END PRIVATE KEY-----
"""

PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAt7LSNwvSvjMMTrvzo8iv
YzEyGyYJJY3ofpS0lU28d5Wq03WyOJ1099rQ7vK5CrGgSouDgMEwLJoyPu78ynxJ
YcW+zp4+jqJEkqpuSPTcbPLWUhQZFUtVoFalwX2PKSe7OAnNL2mBkC6gwLJFvjm3
dUvmBUgPPSwizctbIJO1Z6Zjo6RSV2KMpJrTlJfMBfQZ8A1iDVPY0VIASwaCu6q8
/h9hSUZGKvotWHn8VZ9etfgf7YtZDMMSvh8jvaH+7fknR678yYbE4iOIpQFq7x4j
S3HIGfK00t8jfNdJoIzqX7hfnNtMaFOrK7ihYQY3KMKfbBBoTLovRw5VR43ea1Kx
lwc+iPNqGlfokyYI9Fpkh+3clMAeW5e7634GSU4BeWR3RXu4hKkcKw8jtYFcIXzO
MmxMu4I/9whKckeadjK4czVmfBeZBYVH4wkAd9o8J6tWqonXil2TqHQcp1iLDk3p
qUXtnqyQhrx3lkLu3tOkg2pjsqbTjGuQd8JBdOqqO1esmjSkJrhz8fN/kTWzpiDa
XG2JI9BfimwcqgndAMJH8OOuXAuSPPKN8kYeMJ/a61kD5B4nX6eI+cqKg/cPfGPa
pQE7GFSqaM79AL240M5yhoYhFLVGtqeg/vfEb/V1+hqdmTJmvANAvkrlmoxfcxdx
cF38UF2egml5YnqtcLl3/ukCAwEAAQ==
-----END PUBLIC KEY-----
"""


class FirmServerTestBase:
    async def test_dereference_noauth(self, client):
        tenant = list(client.app.state.tenants.values())[0]
        await tenant.public_store.put({"id": f"{TENANT_PREFIX}/actor/steve", "type": "Person"})
        response = client.get(
            f"{TENANT_PREFIX}/actor/steve",
            headers={"Accept": "application/activity+json"},
        )
        assert response.is_success
        data = response.json()
        assert data["id"] == f"{TENANT_PREFIX}/actor/steve"


class TestServer(FirmServerTestBase): ...


@pytest.mark.skip_unless("FIRM_LIVE_TEST" in os.environ, reason="Not a live test")
class TestLiveServer(FirmServerTestBase): ...


async def test_http_transport_get(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        method="GET",
        json={
            "id": "https://remote.test/actor/bob",
        },
    )
    transport = HttpxTransport()
    response = await transport.get("https://remote.test/actor/bob")
    assert response.status_code == 200
    data = response.json
    assert data["id"] == "https://remote.test/actor/bob"


async def test_http_transport_post(httpx_mock: HTTPXMock):
    httpx_mock.add_response(method="POST", status_code=403)
    transport = HttpxTransport()
    response = await transport.post(
        f"{TENANT_PREFIX}/actor/steve/inbox",
        data={"test": "data"},
        auth=HttpSignatureAuth(f"{TENANT_PREFIX}/actor/bob", PRIVATE_KEY),
    )
    assert response.status_code == 403
