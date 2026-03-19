import pytest
from fastapi import HTTPException, Request
from starlette.testclient import TestClient

import firm.server.server as server_module
from firm.core.interfaces import APActor, Principal
from firm.server.auth import get_principal
from firm.server.config import FileStoreConfig, ServerConfig
from firm.streaming.sse.notifier import InMemoryStreamNotifier, Subscription

TENANT_PREFIX = "https://streaming.test"


@pytest.fixture
def client(tmp_path):
    prefix = TENANT_PREFIX
    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    server_module._app = None
    with TestClient(
        server_module.app_factory(ServerConfig([prefix], FileStoreConfig(base=tmp_path))),
        base_url=prefix,
        raise_server_exceptions=False,
    ) as test_client:
        yield test_client
    server_module._app = None


@pytest.fixture
def authenticated_client(client: TestClient):
    async def _principal_override(request: Request) -> Principal:
        actor = APActor(
            id=f"{TENANT_PREFIX}/actor/test-user",
            type="Person",
            inbox=f"{TENANT_PREFIX}/actor/test-user/inbox",
            outbox=f"{TENANT_PREFIX}/actor/test-user/outbox",
            followers=f"{TENANT_PREFIX}/actor/test-user/followers",
            following=f"{TENANT_PREFIX}/actor/test-user/following",
            likes=f"{TENANT_PREFIX}/actor/test-user/likes",
        )
        return Principal(actor, request.state.tenant)

    client.app.dependency_overrides[get_principal] = _principal_override
    yield client
    client.app.dependency_overrides.pop(get_principal, None)


@pytest.mark.parametrize(
    "topic, published_topic, expected_match",
    [
        ("foo", "foo", True),
        ("fooo", "foo", False),
        ("foo/bar", "foo/bar", True),
        ("foo/bar/", "foo/bar", False),
        ("foo/+", "foo/bar", True),
        ("foo/+", "foo/bar", True),
        ("foo/+/baz", "foo/bar/baz", True),
        ("foo/+/baz", "foo/bar", False),
        ("foo/+/baz/+/quux", "foo/bar/baz/quux", False),
        ("foo/+/baz/+/quux", "foo/bar/baz/zab/quux", True),
        ("foo/#", "foo/bar/baz", True),
        ("foo/.*", "foo/.*", True),
        ("foo/.*", "foo/bar", False),
    ],
)
def test_subscription_matching(topic, published_topic, expected_match):
    assert Subscription(topic).is_match(published_topic) == expected_match


@pytest.mark.parametrize(
    "topic, error",
    [
        ("foo/#/baz", "must be at the end"),
        ("foo/+baz", "must be full segment"),
        ("server.example/foo#2", "must be full segment"),
    ],
)
def test_subscription_errors(topic, error):
    with pytest.raises(ValueError) as ex:
        Subscription(topic)
    assert error in str(ex.value)


async def test_notifier():
    notifier = InMemoryStreamNotifier()
    await notifier.add_subscription("user1", "foo/bar")
    assert await notifier.has_subscriptions("user1")
    await notifier.notify("foo/bar", {"message": "Hello, world!"})
    user1_stream = notifier.notification_stream("user1")
    assert await user1_stream.__anext__()
    await notifier.remove_subscription("user1", "foo/bar")
    assert not await notifier.has_subscriptions("user1")
    await notifier.notify("foo/bar", {"message": "This should not be received"})
    assert not await notifier.has_events("user1")


def test_sse_control_requires_authentication(client: TestClient):
    response = client.post("/sse/control")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Basic"


def test_sse_control_create_session_sets_ticket_cookie(authenticated_client: TestClient):
    response = authenticated_client.post("/sse/control")
    assert response.status_code == 201
    body = response.json()
    assert len(body) == 4
    assert body["subscriptions_url"] == "/sse/control/subscriptions"
    assert body["stream_url"] == "/sse/stream"
    assert body["wildcard_support"] is True
    assert "expires_at" in body
    assert "sse_ticket" in authenticated_client.cookies


def test_sse_subscription_endpoints_require_valid_ticket(authenticated_client: TestClient):
    response = authenticated_client.get("/sse/control/subscriptions")
    assert response.status_code == 401
    assert "valid SSE session ticket" in response.json()["detail"]


def test_sse_subscription_crud(authenticated_client: TestClient):
    create_session = authenticated_client.post("/sse/control")
    assert create_session.status_code == 201
    subscribe = authenticated_client.post(
        "/sse/control/subscriptions",
        json={"topics": ["foo/#"]},
    )
    assert subscribe.status_code == 200
    assert subscribe.json() == {"topics": ["foo/#"]}

    subscriptions = authenticated_client.get("/sse/control/subscriptions")
    assert subscriptions.status_code == 200
    assert subscriptions.json() == {"topics": ["foo/#"]}

    unsubscribe = authenticated_client.delete(
        "/sse/control/subscriptions",
        params={"topic": "foo/#"},
    )
    assert unsubscribe.status_code == 204

    empty = authenticated_client.get("/sse/control/subscriptions")
    assert empty.status_code == 200
    assert empty.json() == {"topics": []}


def test_sse_stream_requires_valid_ticket(authenticated_client: TestClient):
    with pytest.raises(ExceptionGroup) as ex:
        with TestClient(
            authenticated_client.app,
            base_url=str(authenticated_client.base_url),
            raise_server_exceptions=True,
        ) as strict_client:
            strict_client.get("/sse/stream")

    http_exception = ex.value.exceptions[0]
    assert isinstance(http_exception, HTTPException)
    assert http_exception.status_code == 401
    assert "Invalid or expired SSE ticket" in http_exception.detail


def test_sse_control_delete_revokes_session(authenticated_client: TestClient):
    create_session = authenticated_client.post("/sse/control")
    assert create_session.status_code == 201
    revoke = authenticated_client.delete("/sse/control")
    assert revoke.status_code == 204
    response = authenticated_client.get("/sse/control/subscriptions")
    assert response.status_code == 401
