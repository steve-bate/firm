from http import HTTPStatus
from typing import cast

import pytest

from firm.auth.authorization import CoreAuthorizationService
from firm.interfaces import FIRM_NS, JSONObject, Principal, ResourceStore
from firm.store.memory import MemoryResourceStore
from firm.util import AP_PUBLIC_URIS


@pytest.fixture
def store():
    return MemoryResourceStore()


PRINCIPAL_INBOX_URI = "https://server.test/user/1/inbox"
PRINCIPAL_OUTBOX_URI = "https://server.test/user/1/outbox"


@pytest.fixture
async def principal(store: ResourceStore):
    principal = Principal(
        actor={
            "type": "Person",
            "id": "https://server.test/user/1",
            "inbox": PRINCIPAL_INBOX_URI,
            "outbox": PRINCIPAL_OUTBOX_URI,
            "followers": "https://server.test/user/1/followers",
            "following": "https://server.test/user/1/following",
            "likes": "https://server.test/user/1/likes",
        }
    )
    await store.put(cast(JSONObject, principal.actor))
    return principal


async def test_get_not_public_no_authz(
    store: ResourceStore,
):
    authz = CoreAuthorizationService("https://server.test", store)
    resource: JSONObject = {
        "type": "Note",
        "id": "https://server.test/user/1/note/1",
        "content": "Hello, world!",
    }
    decision = await authz.is_get_authorized(None, resource)
    assert not decision.authorized
    assert decision.status_code == HTTPStatus.UNAUTHORIZED.value


async def test_get_not_public_not_attributed(
    store: ResourceStore,
    principal: Principal,
):
    authz = CoreAuthorizationService("https://server.test", store)
    resource: JSONObject = {
        "type": "Note",
        "id": "https://server.test/user/1/note/1",
        "content": "Hello, world!",
    }
    decision = await authz.is_get_authorized(principal, resource)
    assert not decision.authorized


async def test_get_public_not_attributed(
    store: ResourceStore,
    principal: Principal,
):
    authz = CoreAuthorizationService("https://server.test", store)
    resource: JSONObject = {
        "type": "Note",
        "id": "https://server.test/user/1/note/1",
        "audience": AP_PUBLIC_URIS[0],
        "content": "Hello, world!",
    }
    decision = await authz.is_get_authorized(principal, resource)
    assert decision.authorized


async def test_get_actor_resource(
    store: ResourceStore,
    principal: Principal,
):
    authz = CoreAuthorizationService("https://server.test", store)
    resource: JSONObject = {
        "type": "Application",
        "id": "https://server.test/user/1/app/1",
    }
    decision = await authz.is_get_authorized(principal, resource)
    assert decision.authorized


async def test_get_attributed_to_user(
    store: ResourceStore,
    principal: Principal,
):
    authz = CoreAuthorizationService("https://server.test", store)
    resource: JSONObject = {
        "type": "Note",
        "id": "https://server.test/user/1/note/1",
        "attributedTo": principal.actor["id"],
        "content": "Hello, world!",
    }
    decision = await authz.is_get_authorized(principal, resource)
    assert decision.authorized


async def test_get_outbox(
    store: ResourceStore,
    principal: Principal,
):
    authz = CoreAuthorizationService("https://server.test", store)
    resource: JSONObject = {
        "type": "OrderedCollection",
        "id": "https://server.test/user/1/outbox",
    }
    await store.put(resource)
    decision = await authz.is_get_authorized(principal, resource)
    assert decision.authorized


async def test_get_requester_inbox(
    store: ResourceStore,
    principal: Principal,
):
    authz = CoreAuthorizationService("https://server.test", store)
    resource: JSONObject = {
        "type": "OrderedCollection",
        "id": "https://server.test/user/1/inbox",
    }
    await store.put(resource)
    decision = await authz.is_get_authorized(principal, resource)
    assert decision.authorized


async def test_get_unowned_inbox(
    store: ResourceStore,
    principal: Principal,
):
    authz = CoreAuthorizationService("https://server.test", store)
    resource: JSONObject = {
        "type": "OrderedCollection",
        "id": "https://server.test/user/OTHER/inbox",
    }
    await store.put(resource)
    decision = await authz.is_get_authorized(principal, resource)
    assert not decision.authorized
    assert decision.status_code == HTTPStatus.FORBIDDEN.value


async def test_get_owned_activity(
    store: ResourceStore,
    principal: Principal,
):
    authz = CoreAuthorizationService("https://server.test", store)
    resource: JSONObject = {
        "type": "Create",
        "actor": principal.actor["id"],
    }
    await store.put(resource)
    decision = await authz.is_get_authorized(principal, resource)
    assert decision.authorized


async def test_get_unowned_activity(
    store: ResourceStore,
    principal: Principal,
):
    authz = CoreAuthorizationService("https://server.test", store)
    resource: JSONObject = {
        "type": "Create",
        "actor": "https://server.test/user/OTHER",
    }
    await store.put(resource)
    decision = await authz.is_get_authorized(principal, resource)
    assert not decision.authorized
    assert decision.status_code == HTTPStatus.FORBIDDEN.value


async def test_get_when_recipient(
    store: ResourceStore,
    principal: Principal,
):
    authz = CoreAuthorizationService("https://server.test", store)
    resource: JSONObject = {
        "type": "Create",
        "actor": "https://server.test/user/OTHER",
        "to": principal.actor["id"],
    }
    await store.put(resource)
    decision = await authz.is_get_authorized(principal, resource)
    assert decision.authorized


async def test_get_when_not_recipient(
    store: ResourceStore,
    principal: Principal,
):
    authz = CoreAuthorizationService("https://server.test", store)
    resource: JSONObject = {
        "type": "Create",
        "actor": "https://server.test/user/OTHER",
        "to": principal.actor["id"],
    }
    await store.put(resource)
    decision = await authz.is_get_authorized(principal, resource)
    assert decision.authorized


async def test_get_when_not_owner_or_recipient(
    store: ResourceStore,
    principal: Principal,
):
    authz = CoreAuthorizationService("https://server.test", store)
    resource: JSONObject = {"type": "Create", "to": "https://server.test/user/OTHER"}
    await store.put(resource)
    decision = await authz.is_get_authorized(principal, resource)
    assert not decision.authorized
    assert decision.status_code == HTTPStatus.FORBIDDEN.value


@pytest.mark.parametrize(
    "inbox_uri",
    [
        PRINCIPAL_INBOX_URI,
        "https://server.test/user/OTHER/inbox",
    ],
)
async def test_post_activity_to_inbox(
    store: ResourceStore,
    principal: Principal,
    inbox_uri: str,
):
    authz = CoreAuthorizationService("https://server.test", store)
    decision = await authz.is_post_authorized(principal, "inbox", inbox_uri)
    assert decision.authorized


@pytest.mark.parametrize(
    ["outbox_uri", "authorized", "status_code"],
    [
        (PRINCIPAL_OUTBOX_URI, True, 200),
        ("https://server.test/user/OTHER/outbox", False, 403),
    ],
)
async def test_post_activity_to_outbox(
    store: ResourceStore,
    principal: Principal,
    outbox_uri: str,
    authorized: bool,
    status_code: int,
):
    authz = CoreAuthorizationService("https://server.test", store)
    decision = await authz.is_post_authorized(principal, "outbox", outbox_uri)
    assert decision.authorized == authorized
    assert authorized or decision.status_code == status_code


async def test_post_activity_to_outbox_without_auth(store: ResourceStore):
    authz = CoreAuthorizationService("https://server.test", store)
    decision = await authz.is_post_authorized(None, "outbox", "https://server.test/box")
    assert not decision.authorized
    assert decision.status_code == HTTPStatus.UNAUTHORIZED.value


@pytest.mark.parametrize(
    ["activity_type", "error"],
    [(t, e) for t in ["Add", "Remove"] for e in ["missing-object", "missing-target"]],
)
async def test_activity_add_remove_missing_data(
    store: ResourceStore,
    principal: Principal,
    activity_type: str,
    error: str,
):
    authz = CoreAuthorizationService("https://server.test", store)
    activity: JSONObject = {
        "type": activity_type,
        "actor": principal.actor["id"],
        "object": "https://server.test/object/1",
        "target": "https://server.test/collection/1",
    }
    if error == "missing-object":
        activity.pop("object")
    if error == "missing-target":
        activity.pop("target")
    decision = await authz.is_activity_authorized(principal, activity)
    assert not decision.authorized
    assert decision.status_code == 400


@pytest.mark.parametrize(
    ["activity_type", "scenario"],
    [
        (t, e)
        for t in ["Add", "Remove"]
        for e in ["public-target", "attributed-to", "actor-collection"]
    ],
)
async def test_activity_add_remove(
    store: ResourceStore,
    principal: Principal,
    activity_type: str,
    scenario: str,
):
    authz = CoreAuthorizationService("https://server.test", store)
    target: JSONObject = {
        "id": "https://server.test/collection/1",
        "type": "Collection",
    }
    await store.put(target)
    activity: JSONObject = {
        "type": activity_type,
        "actor": principal.actor["id"],
        "object": "https://server.test/object/1",
        "target": "https://server.test/collection/1",
    }
    if scenario == "public-target":
        target["audience"] = AP_PUBLIC_URIS[0]
    elif scenario == "attributed-to":
        target["attributedTo"] = principal.actor["id"]
    elif scenario == "actor-collection":
        target["id"] = principal.actor["followers"]
    decision = await authz.is_activity_authorized(principal, activity)
    assert decision.authorized


@pytest.mark.parametrize("activity_type", ["Add", "Remove"])
async def test_activity_add_remove_noauthz(
    store: ResourceStore,
    principal: Principal,
    activity_type: str,
):
    authz = CoreAuthorizationService("https://server.test", store)
    target: JSONObject = {
        "id": "https://server.test/collection/1",
        "type": "Collection",
    }
    await store.put(target)
    activity: JSONObject = {
        "type": activity_type,
        "actor": "https://server.test/user/OTHER",
        "object": "https://server.test/object/1",
        "target": "https://server.test/collection/1",
    }
    decision = await authz.is_activity_authorized(principal, activity)
    assert not decision.authorized
    assert decision.status_code == HTTPStatus.FORBIDDEN.value


async def test_instance_level_domain_blocking_post(
    store: ResourceStore, principal: Principal
):
    authz = CoreAuthorizationService("http://tenant1.test", store)

    await store.put(
        {
            "id": "http://tenant1.test/block",
            "type": FIRM_NS.Blocks,
            "attributedTo": "http://tenant1.test",
            FIRM_NS.blockedDomain: "server.test",
        }
    )

    decision = await authz.is_post_authorized(
        principal, "inbox", "https://remote.test/inbox"
    )

    assert not decision.authorized
    assert decision.status_code == HTTPStatus.FORBIDDEN.value
    assert "blocked" in str(decision.reason)


async def test_instance_level_domain_blocking_get(
    store: ResourceStore, principal: Principal
):
    authz = CoreAuthorizationService("http://tenant1.test", store)

    activity: JSONObject = {
        "id": "https://server.test/object/1",
        "type": "Follow",
        "to": "https://server.test/user/1",
        "actor": "https://server.test/user/OTHER",
        "object": "https://server.test/object/1",
        "target": "https://server.test/collection/1",
    }

    await store.put(
        {
            "id": "http://tenant1.test/block",
            "type": FIRM_NS.Blocks,
            "attributedTo": "http://tenant1.test",
            FIRM_NS.blockedDomain: "server.test",
        }
    )

    decision = await authz.is_get_authorized(principal, activity)

    assert not decision.authorized
    assert decision.status_code == HTTPStatus.FORBIDDEN.value
    assert "blocked" in str(decision.reason)


# async def test_actor_level_domain_blocking(
#     store: ResourceStore, principal: Principal
# ):
#     authz = CoreAuthorizationService("http://tenant1.test", store)

#     activity: JSONObject = {
#         "id": "https://server.test/object/1",
#         "type": "Follow",
#         "to": "https://server.test/user/1",
#         "actor": "https://server.test/user/OTHER",
#         "object": "https://server.test/object/1",
#         "target": "https://server.test/collection/1",
#     }

#     await store.put(
#         {
#             "id": "http://tenant1.test/block",
#             "type": FIRM_NS.Blocks,
#             "attributedTo": "http://tenant1.test",
#             FIRM_NS.blockedDomain: "server.test",
#         }
#     )

#     decision = await authz.is_activity_authorized(
#         principal, activity
#     )

#     assert not decision.authorized
#     assert decision.status_code == HTTPStatus.FORBIDDEN.value
#     assert "blocked" in str(decision.reason)
