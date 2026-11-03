import base64
from hashlib import sha256

import pytest

from firm.core.auth.bearer_token import BearerTokenAuthenticator
from firm.core.auth.chained import AuthenticatorChain
from firm.core.auth.http_basic import BasicHttpAuthenticator, hash_password
from firm.core.auth.http_signature import HttpSigAuthenticator, HttpSignatureAuth
from firm.core.auth.keys import create_key_pair
from firm.core.interfaces import FIRM_NS, JSONObject, Tenant
from firm.core.remote.cache import MemoryRemoteCache
from firm.core.remote.provider import FetchPolicy, RemoteResourceProvider
from firm.core.resolver import ResourceResolver
from firm.core.store.memory import MemoryResourceStore
from tests.support import (
    StubHttpRequest,
    StubIdentity,
    StubTransport,
    json_response,
)

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


REMOTE_ACTOR = "http://remote.test/users/bob"
REMOTE_KEY_ID = f"{REMOTE_ACTOR}#main-key"


def _provider(transport: StubTransport) -> RemoteResourceProvider:
    return RemoteResourceProvider(
        MemoryRemoteCache(), transport, policy=FetchPolicy(allow_private_addresses=True)
    )


def _remote_actor(key_pair, actor_uri: str = REMOTE_ACTOR) -> JSONObject:
    return {
        "id": actor_uri,
        "type": "Person",
        "publicKey": {
            "id": f"{actor_uri}#main-key",
            "owner": actor_uri,
            "publicKeyPem": key_pair.public,
        },
    }


def _signed_request(
    tenant: Tenant, key_id: str, private_key: str, body: bytes = b""
) -> StubHttpRequest:
    """A request signed the way an outbound firm request is: host, date, and digest."""
    headers = {"host": "tenant1.test", "date": "Tue, 03 Nov 2026 12:00:00 GMT"}
    if body:
        headers["digest"] = "SHA-256=" + base64.b64encode(sha256(body).digest()).decode()
    request = StubHttpRequest(
        "POST" if body else "GET",
        "http://tenant1.test/",
        headers=headers,
        body=body,
        tenant=tenant,
    )
    HttpSignatureAuth(key_id, private_key).sign(request)
    return request


@pytest.fixture
def tenant(tmp_path):
    return Tenant(
        "http://tenant1.test",
        MemoryResourceStore(),
        MemoryResourceStore(),
        tmp_path,
    )


@pytest.fixture
def identity(tenant: Tenant):
    return StubIdentity(
        "http://tenant1.test/user1",
        tenant,
    )


async def test_httpsig_sign_verify(tenant: Tenant):
    key_id = "http://server.test/user#main-key"
    actor_uri = "http://server.test/user"
    await tenant.public_store.put({"id": key_id, "owner": actor_uri, "publicKeyPem": PUBLIC_KEY})
    await tenant.public_store.put({"id": actor_uri, "preferredUsername": "bob"})
    signer = HttpSignatureAuth(key_id, PRIVATE_KEY)
    request = StubHttpRequest(
        "GET",
        "http://server.test/",
        headers={"host": "server.test", "date": "2000-01-01T00:00:00Z"},
        body=b"",
        tenant=tenant,
    )
    signer.sign(request)
    # Now verify the signed signature
    assert "Signature" in request.headers
    verifier = HttpSigAuthenticator()
    principal = await verifier.authenticate(request)
    assert principal is not None
    assert principal.actor["id"] == actor_uri


async def test_httpsig_failed(tenant: Tenant):
    key_id = "http://server.test/user#main-key"
    actor_uri = "http://server.test/user"
    await tenant.public_store.put({"id": key_id, "owner": actor_uri, "publicKeyPem": PUBLIC_KEY})
    await tenant.public_store.put({"id": actor_uri, "preferredUsername": "bob"})
    # Incorrect key
    signer = HttpSignatureAuth(key_id, PRIVATE_KEY)
    request = StubHttpRequest(
        "GET",
        "http://server.test/",
        headers={"host": "server.test", "date": "2000-01-01T00:00:00Z"},
        body=b"",
        tenant=tenant,
    )
    signer.sign(request)
    signature = request.headers["Signature"]
    # Change a signature-related header
    # Signed a GET but this is a POST
    request = StubHttpRequest(
        "POST",
        "http://server.test/",
        headers={"host": "server.test", "date": "2000-01-01T00:00:00Z"},
        body=b"",
        tenant=tenant,
    )
    request.headers["Signature"] = signature
    # Now verify the signed signature
    assert "Signature" in request.headers
    verifier = HttpSigAuthenticator()
    principal = await verifier.authenticate(request)
    assert principal is None


async def test_httpsig_no_sig():
    key_id = "http://server.test/user#main-key"
    actor_uri = "http://server.test/user"
    store = MemoryResourceStore()
    await store.put({"id": key_id, "owner": actor_uri, "publicKeyPem": PUBLIC_KEY})
    await store.put({"id": actor_uri, "preferredUsername": "bob"})
    request = StubHttpRequest("GET", "http://server.test/", headers={})
    assert "Signature" not in request.headers
    verifier = HttpSigAuthenticator()
    principal = await verifier.authenticate(request)
    assert principal is None  # Unauthorized


async def test_httpsig_remote_actor_verifies_from_the_provider(tenant: Tenant):
    key_pair = create_key_pair()
    transport = StubTransport({REMOTE_ACTOR: json_response(_remote_actor(key_pair))})
    resolver = ResourceResolver({tenant.prefix: tenant}, _provider(transport))
    request = _signed_request(tenant, REMOTE_KEY_ID, key_pair.private, b'{"hello": "world"}')

    principal = await HttpSigAuthenticator(resolver=resolver).authenticate(request)

    assert principal is not None
    assert principal.actor["id"] == REMOTE_ACTOR
    assert transport.urls == [REMOTE_ACTOR], "fetched from the host that owns the key"
    assert await tenant.public_store.get(REMOTE_ACTOR) is None, "cached, never tenant-stored"


async def test_httpsig_remote_key_is_fetched_once(tenant: Tenant):
    key_pair = create_key_pair()
    transport = StubTransport({REMOTE_ACTOR: json_response(_remote_actor(key_pair))})
    resolver = ResourceResolver({tenant.prefix: tenant}, _provider(transport))
    verifier = HttpSigAuthenticator(resolver=resolver)

    for _ in range(2):
        request = _signed_request(tenant, REMOTE_KEY_ID, key_pair.private)
        assert await verifier.authenticate(request) is not None

    assert transport.urls == [REMOTE_ACTOR], "the second POST is served from the cache"


async def test_httpsig_local_actor_verifies_without_a_fetch(tenant: Tenant):
    key_pair = create_key_pair()
    key_id = f"{tenant.prefix}/user#main-key"
    actor_uri = f"{tenant.prefix}/user"
    await tenant.public_store.put(
        {"id": key_id, "owner": actor_uri, "publicKeyPem": key_pair.public}
    )
    await tenant.public_store.put({"id": actor_uri, "type": "Person"})
    transport = StubTransport({})
    resolver = ResourceResolver({tenant.prefix: tenant}, _provider(transport))

    principal = await HttpSigAuthenticator(resolver=resolver).authenticate(
        _signed_request(tenant, key_id, key_pair.private)
    )

    assert principal is not None and principal.actor["id"] == actor_uri
    assert transport.urls == [], "a local actor costs no network call"


async def test_httpsig_actor_in_another_tenant_verifies_without_copy(tenant: Tenant, tmp_path):
    other = Tenant("http://tenant2.test", MemoryResourceStore(), MemoryResourceStore(), tmp_path)
    key_pair = create_key_pair()
    actor_uri = "http://tenant2.test/users/carol"
    key_id = f"{actor_uri}#main-key"
    await other.public_store.put(
        {"id": key_id, "owner": actor_uri, "publicKeyPem": key_pair.public}
    )
    await other.public_store.put({"id": actor_uri, "type": "Person"})
    resolver = ResourceResolver({tenant.prefix: tenant, other.prefix: other})

    principal = await HttpSigAuthenticator(resolver=resolver).authenticate(
        _signed_request(tenant, key_id, key_pair.private)
    )

    assert principal is not None and principal.actor["id"] == actor_uri
    assert await tenant.public_store.get(actor_uri) is None, "not copied into the receiver"


async def test_httpsig_malformed_signature_is_unauthenticated(tenant: Tenant):
    request = StubHttpRequest(
        "GET", "http://tenant1.test/", headers={"Signature": "nonsense"}, tenant=tenant
    )

    assert await HttpSigAuthenticator().authenticate(request) is None


async def test_httpsig_unparseable_key_is_unauthenticated(tenant: Tenant):
    key_pair = create_key_pair()
    actor_uri = f"{tenant.prefix}/broken"
    key_id = f"{actor_uri}#main-key"
    await tenant.public_store.put(
        {"id": key_id, "owner": actor_uri, "publicKeyPem": "not a PEM key"}
    )
    await tenant.public_store.put({"id": actor_uri, "type": "Person"})

    principal = await HttpSigAuthenticator().authenticate(
        _signed_request(tenant, key_id, key_pair.private)
    )

    assert principal is None


async def test_httpsig_remote_key_cannot_claim_an_unrelated_owner(tenant: Tenant):
    key_pair = create_key_pair()
    local_actor = f"{tenant.prefix}/local-actor"
    await tenant.public_store.put({"id": local_actor, "type": "Person"})
    key_id = "http://evil.test/keys/1"
    key_document: JSONObject = {
        "id": key_id,
        "owner": local_actor,
        "publicKeyPem": key_pair.public,
    }
    transport = StubTransport({key_id: json_response(key_document)})
    resolver = ResourceResolver({tenant.prefix: tenant}, _provider(transport))

    principal = await HttpSigAuthenticator(resolver=resolver).authenticate(
        _signed_request(tenant, key_id, key_pair.private)
    )

    assert principal is None, "a foreign key may not claim a local actor"


async def test_httpsig_digest_must_match_the_body(tenant: Tenant):
    key_pair = create_key_pair()
    actor_uri = f"{tenant.prefix}/dana"
    key_id = f"{actor_uri}#main-key"
    await tenant.public_store.put(
        {"id": key_id, "owner": actor_uri, "publicKeyPem": key_pair.public}
    )
    await tenant.public_store.put({"id": actor_uri, "type": "Person"})
    signed = _signed_request(tenant, key_id, key_pair.private, b'{"a": 1}')
    tampered = StubHttpRequest(
        "POST",
        "http://tenant1.test/",
        headers=dict(signed.headers),
        body=b'{"a": 2}',
        tenant=tenant,
    )

    assert await HttpSigAuthenticator().authenticate(tampered) is None


async def test_httpsig_digest_must_be_in_the_signed_headers(tenant: Tenant):
    key_pair = create_key_pair()
    actor_uri = f"{tenant.prefix}/dana"
    key_id = f"{actor_uri}#main-key"
    await tenant.public_store.put(
        {"id": key_id, "owner": actor_uri, "publicKeyPem": key_pair.public}
    )
    await tenant.public_store.put({"id": actor_uri, "type": "Person"})
    body = b'{"a": 1}'
    request = StubHttpRequest(
        "POST",
        "http://tenant1.test/",
        headers={
            "host": "tenant1.test",
            "date": "Tue, 03 Nov 2026 12:00:00 GMT",
            "digest": "SHA-256=" + base64.b64encode(sha256(body).digest()).decode(),
        },
        body=body,
        tenant=tenant,
    )
    HttpSignatureAuth(key_id, key_pair.private, headers=["(request-target)", "host", "date"]).sign(
        request
    )

    assert await HttpSigAuthenticator().authenticate(request) is None


async def test_bearer_token(tenant: Tenant):
    actor_uri = "http://server.test/user"
    await tenant.public_store.put({"id": actor_uri})
    await tenant.private_store.put(
        {
            "id": "urn:uuid:1234",
            "attributedTo": actor_uri,
            "type": FIRM_NS.Credentials.value,
            FIRM_NS.token.value: "ABCD",
        }
    )
    auth = BearerTokenAuthenticator()
    request = StubHttpRequest(
        "GET",
        "http://server.test/",
        headers={"Authorization": "Bearer ABCD"},
        tenant=tenant,
    )
    principal = await auth.authenticate(request)
    assert principal is not None
    assert principal.actor["id"] == actor_uri


async def test_bearer_wrong_token(tenant: Tenant):
    actor_uri = "http://server.test/user"
    await tenant.private_store.put({"id": actor_uri, FIRM_NS.token.value: "ABCD"})
    auth = BearerTokenAuthenticator()
    request = StubHttpRequest(
        "GET",
        "http://server.test/",
        headers={"Authorization": "Bearer XYWZ"},
        tenant=tenant,
    )
    principal = await auth.authenticate(request)
    assert principal is None


async def test_bearer_missing_token():
    actor_uri = "http://server.test/user"
    store = MemoryResourceStore()
    await store.put({"id": actor_uri, FIRM_NS.token.value: "ABCD"})
    auth = BearerTokenAuthenticator()
    request = StubHttpRequest("GET", "http://server.test/", store=store)
    principal = await auth.authenticate(request)
    assert principal is None


async def test_basic_auth(tenant: Tenant):
    actor_uri = "http://server.test/user"
    password = "letmein"
    hashed_password = hash_password(password)
    await tenant.public_store.put({"id": actor_uri})
    await tenant.private_store.put(
        {
            "id": "urn:uuid:1234",
            "attributedTo": actor_uri,
            "type": FIRM_NS.Credentials.value,
            FIRM_NS.password.value: hashed_password,
        }
    )
    auth = BasicHttpAuthenticator()
    auth_header = base64.b64encode(f"{actor_uri}:{password}".encode()).decode()
    request = StubHttpRequest(
        "GET",
        "http://server.test/",
        headers={"Authorization": f"basic {auth_header}"},
        tenant=tenant,
    )
    principal = await auth.authenticate(request)
    assert principal is not None


async def test_basic_auth_wrong_password(tenant: Tenant):
    actor_uri = "http://server.test/user"
    password = "letmein"
    hashed_password = hash_password(password)
    await tenant.private_store.put({"id": actor_uri, FIRM_NS.password.value: hashed_password})
    auth = BasicHttpAuthenticator()
    auth_header = base64.b64encode(f"{actor_uri}:BOGUS".encode()).decode()
    request = StubHttpRequest(
        "GET",
        "http://server.test/",
        headers={"Authorization": f"basic {auth_header}"},
        tenant=tenant,
    )
    principal = await auth.authenticate(request)
    assert principal is None


async def test_basic_auth_missing_header(tenant: Tenant):
    actor_uri = "http://server.test/user"
    password = "letmein"
    hashed_password = hash_password(password)
    await tenant.private_store.put({"id": actor_uri, FIRM_NS.password.value: hashed_password})
    auth = BasicHttpAuthenticator()
    request = StubHttpRequest("GET", "http://server.test/", tenant=tenant)
    principal = await auth.authenticate(request)
    assert principal is None


@pytest.mark.parametrize(
    "password,token,authenticated",
    [
        ("letmein", None, True),
        ("BOGUS", None, False),
        (None, "BOGUS", False),
        (None, "ABCD", True),
        (None, None, False),
    ],
)
async def test_auth_chain(password, token, authenticated, identity: StubIdentity) -> None:
    actor_uri = "http://server.test/user"
    tenant = identity.tenant
    actor: JSONObject = {
        "id": actor_uri,
    }
    await tenant.public_store.put(actor)
    credentials: JSONObject = {
        "id": "urn:uuid:1234",
        "attributedTo": actor_uri,
        "type": FIRM_NS.Credentials.value,
    }
    await tenant.private_store.put(credentials)
    auth = AuthenticatorChain(
        [
            BasicHttpAuthenticator(),
            BearerTokenAuthenticator(),
        ]
    )
    headers: dict[str, str] = {}
    if password:
        hashed_password = hash_password("letmein")
        credentials[FIRM_NS.password.value] = hashed_password
        auth_header = base64.b64encode(f"{actor_uri}:{password}".encode()).decode()
        headers["Authorization"] = f"basic {auth_header}"
    elif token:
        credentials[FIRM_NS.token.value] = "ABCD"
        headers["Authorization"] = f"bearer {token}"
    request = StubHttpRequest("GET", "http://server.test/", headers=headers, auth=identity)
    principal = await auth.authenticate(request)
    assert (principal is not None) == authenticated
