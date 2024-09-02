import base64

import pytest

from firm.auth.bearer_token import BearerTokenAuthenticator
from firm.auth.chained import AuthenticatorChain
from firm.auth.http_basic import BasicHttpAuthenticator, hash_password
from firm.auth.http_signature import HttpSigAuthenticator, HttpSignatureAuth
from firm.interfaces import FIRM_NS, JSONObject
from firm.store.memory import MemoryResourceStore
from tests.support import StubHttpRequest

PRIVATE_KEY = """-----BEGIN PRIVATE KEY-----
MIIJQQIBADANBgkqhkiG9w0BAQEFAASCCSswggknAgEAAoICAQC3stI3C9K+MwxO
u/OjyK9jMTIbJgkljeh+lLSVTbx3larTdbI4nXT32tDu8rkKsaBKi4OAwTAsmjI+
7vzKfElhxb7Onj6OokSSqm5I9Nxs8tZSFBkVS1WgVqXBfY8pJ7s4Cc0vaYGQLqDA
skW+Obd1S+YFSA89LCLNy1sgk7VnpmOjpFJXYoykmtOUl8wF9BnwDWINU9jRUgBL
BoK7qrz+H2FJRkYq+i1YefxVn161+B/ti1kMwxK+HyO9of7t+SdHrvzJhsTiI4il
AWrvHiNLccgZ8rTS3yN810mgjOpfuF+c20xoU6sruKFhBjcowp9sEGhMui9HDlVH
jd5rUrGXBz6I82oaV+iTJgj0WmSH7dyUwB5bl7vrfgZJTgF5ZHdFe7iEqRwrDyO1
gVwhfM4ybEy7gj/3CEpyR5p2MrhzNWZ8F5kFhUfjCQB32jwnq1aqideKXZOodByn
WIsOTempRe2erJCGvHeWQu7e06SDamOyptOMa5B3wkF06qo7V6yaNKQmuHPx83+R
NbOmINpcbYkj0F+KbByqCd0Awkfw465cC5I88o3yRh4wn9rrWQPkHidfp4j5yoqD
9w98Y9qlATsYVKpozv0AvbjQznKGhiEUtUa2p6D+98Rv9XX6Gp2ZMma8A0C+SuWa
jF9zF3FwXfxQXZ6CaXlieq1wuXf+6QIDAQABAoICAFaXn8o884me7KVMqevB1RMw
BIuRoWwnebn5hSqAK2A/l/f4Ghvf9VxEtIp+tkVZN9ML8uBFsNzFjvvlkhos/jZt
jaU+KQT5btOoLTaM3j8pNWgZez1zdpiPX7FW654d0X33+NXpqR57LGHJZ2DlOhq7
vWEt96kBXiKeQoWXu0Jxx7RC6GGy3dNV/HimGZGQ4I0s8dSQerspKWQ0XHn0YQR1
bFmrG7Z0md2EGzONXYrvvLUwI7kFV5dxfFqOu2oYMbDzxsuEkNh8oZQOmAbBsSeG
Kio5I43ni4X0wgtBgdW/RqrdISZokl6YuNHQqT24iIfbMB9DALhBBGgncvoqT/Wx
HomdCxbk823MXpDutL82q5W1UZ5S0VAMvML9knuRfThRPER1pGkIi4eISOYicWTr
BMFHEfSOoH+fRr6gU0B76aa1j2d1Wxdez/bbTEVE01bLC2HbSyoPe7UeRVi4Yboo
fLptl0ZzSNrArB8fj12uVvBPM0K1SjQ2vr//gNdvpoEY18FqbJdcpnmwHMwHJa1G
u9Waq6+fV8Z3OVqio5CLNFWIbYLXGpthw59gWpbqGohhBbT58XAa6Q/86PtpLybi
IiKQH9pH9U0/Xvm6uWPdCaiGehEWEuBoF2LIULKmHJPKe89l9TDEIYQhm7F4U51W
ibL+vqC7c0NcNGxZ2pDFAoIBAQDFIKjaOubcJxWS0zIk+zGhgnn3OzFkrxR8njDD
hXs8S8oOi0ycv0Pi+St1gYmhCXBnOlAxayZhZg1PnXwsH6K34tmEhJe99CiOwLoY
lkl6c2h4GyPPyNEA2lIUPi1dDWNPllww62kFKMyWsRbRw6R1w23p/w4e7i0ramfe
57sUoTMC5eyoUxQI/3Dbc3eFBw/c2PEaV2hygg6VK5bkD+h1vcVpH0miZOpnuEse
S2EuS7E73KRmhw3Grwen4bTYnX+wAF+smSZm9UDg/S9fngb5amS5IhaOLtiMocCK
qhoK499+W3aajHVvf5jtSQNcy+h20PYNU02veycgfe6YNl3vAoIBAQDuj3JFNsJS
A2FAvPHfDOzDm2ihZhBiAWOHxsgVO3z8DasWbU1hAQXsuiFFbzHMC12XHE+fhyFx
LXKV53ccyAZvZIFToLrwEWu4y0IgH4cwPp8qs8RHXSGpBmHhGMWuExGMHXG/W5iv
f8t5lgLYZkrkUBCUhvsK0myn+Ai1vLnIquPwXMxe83uI4ok5RifUpkYsOmRcT0NW
Pt39Tvo2q0po6Spt4uat3Lkb84wxTftOqdNgIe2MciXaZdUTiBn6cPY0inEKfjWJ
6vwXqnW06M/xgAHhhXmsRxSNfmUSWUu5L5qi1f9DqG/cFXuM6Hx5TBh8zSdhTzZ3
UZBrWVwTQsinAoIBABDrWbLJZXE15ZMhj2c/LCZZpZBDw1yJ7m83wKW3ejlVo/UV
nbDCddgwXLuML7zjq4MgrStgr/2iHbhcowDCglvYG6VVIBUMtMJz5kUf+RSKfUf5
xFwcN1wkYPEd2RTohkKZfDYyrmPj+ZNhhbzhVudIq9Fus86R0MyuKFYoe5UstM0l
4OcdolWXXx9mzLZdQc5JzH/fSraxVQEWqa/PcbtRW3VHWzGWCcx3M/NYsvGfS4oA
yReHtfX8peKR68y/z+rSTWPqDTK/EB9/e6ZwUNbte9GsDFWNzcZcR8NfEDcpEdCt
lwNy1M2KHR0YrDI1yjEQhF3mbX+HSXdvd6AW4n8CggEABeAGgmnc00Q+CugcVM/u
rMqRAxiOYruCBgABQXSbmWGEyyKZ+z+ZM8FJvHoGke3dujD6TQV4716dKc/vgQf0
EJ47CSI2OF9VddGbqUrde3SvWs/ej5tdjtoXYwHHLIhPsFGxUXMiCYBuNGpbW5T5
VzIZlm7Uk+mmv2Q+YqtpL+X1gx/l8JiyfCaIFp8BsB0AMWqmuhdBo0gdE3X0d5A0
Xu0PHHGwGKwM6wFOfJBdFgzcpctwHDtbb0t+ueJqMV7C0XxvWEDPdLwSxUpvZ6ss
I9hxM2qkGngNq4ZnWtJUKRVhC42VocbuKk9lIY1AM4SKPdiXla/ruXiKw/oJaHgG
lQKCAQAKfCrDvCFcgjp8ffesHzJG7/rJxRGDQbHRV3el26sSIdKs77ETrel6gaVA
7ISTfyNGvS8hC/msl5d2GgVyGWZMnpfbBsKb2J9T55nfiA/JxQjKw/WwnGg0Rhmo
rb3Yc05VWyRBiEQQsfzFYPoJ88pxeCWPzBRblPcDLuvnKrem0i9XnloJJWk2mv7Z
7yRNvYr8hCQcLnXrouXiapkk92AdwkzeD3gDwZpPzEPiAlmOk65MwlweUQabxa1V
ytOxXLfcbU8oyN2wkDYYSNDx/kWgb3dG2Im6yHRTsCm99GkyzgiaXnCMAnhRjLwR
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


async def test_httpsig_sign_verify():
    key_id = "http://server.test/user#main-key"
    actor_uri = "http://server.test/user"
    store = MemoryResourceStore()
    await store.put({"id": key_id, "owner": actor_uri, "publicKeyPem": PUBLIC_KEY})
    await store.put({"id": actor_uri, "preferredUsername": "bob"})
    signer = HttpSignatureAuth(key_id, PRIVATE_KEY)
    request = StubHttpRequest(
        "GET",
        "http://server.test/",
        headers={"host": "server.test", "date": "2000-01-01T00:00:00Z"},
        body=b"",
        store=store,
    )
    signer.sign(request)
    # Now verify the signed signature
    assert "Signature" in request.headers
    verifier = HttpSigAuthenticator()
    principal = await verifier.authenticate(request)
    assert principal is not None
    assert principal.actor["id"] == actor_uri


async def test_httpsig_failed():
    key_id = "http://server.test/user#main-key"
    actor_uri = "http://server.test/user"
    store = MemoryResourceStore()
    await store.put({"id": key_id, "owner": actor_uri, "publicKeyPem": PUBLIC_KEY})
    await store.put({"id": actor_uri, "preferredUsername": "bob"})
    # Incorrect key
    signer = HttpSignatureAuth(key_id, PRIVATE_KEY)
    request = StubHttpRequest(
        "GET",
        "http://server.test/",
        headers={"host": "server.test", "date": "2000-01-01T00:00:00Z"},
        body=b"",
        store=store,
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
        store=store,
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


async def test_bearer_token():
    actor_uri = "http://server.test/user"
    store = MemoryResourceStore()
    await store.put({"id": actor_uri})
    await store.put(
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
        store=store,
    )
    principal = await auth.authenticate(request)
    assert principal is not None
    assert principal.actor["id"] == actor_uri


async def test_bearer_wrong_token():
    actor_uri = "http://server.test/user"
    store = MemoryResourceStore()
    await store.put({"id": actor_uri, FIRM_NS.token.value: "ABCD"})
    auth = BearerTokenAuthenticator()
    request = StubHttpRequest(
        "GET",
        "http://server.test/",
        headers={"Authorization": "Bearer XYWZ"},
        store=store,
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


async def test_basic_auth():
    actor_uri = "http://server.test/user"
    store = MemoryResourceStore()
    password = "letmein"
    hashed_password = hash_password(password)
    await store.put({"id": actor_uri})
    await store.put(
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
        store=store,
    )
    principal = await auth.authenticate(request)
    assert principal is not None


async def test_basic_auth_wrong_password():
    actor_uri = "http://server.test/user"
    store = MemoryResourceStore()
    password = "letmein"
    hashed_password = hash_password(password)
    await store.put({"id": actor_uri, FIRM_NS.password.value: hashed_password})
    auth = BasicHttpAuthenticator()
    auth_header = base64.b64encode(f"{actor_uri}:BOGUS".encode()).decode()
    request = StubHttpRequest(
        "GET",
        "http://server.test/",
        headers={"Authorization": f"basic {auth_header}"},
        store=store,
    )
    principal = await auth.authenticate(request)
    assert principal is None


async def test_basic_auth_missing_header():
    actor_uri = "http://server.test/user"
    store = MemoryResourceStore()
    password = "letmein"
    hashed_password = hash_password(password)
    await store.put({"id": actor_uri, FIRM_NS.password.value: hashed_password})
    auth = BasicHttpAuthenticator()
    request = StubHttpRequest("GET", "http://server.test/", store=store)
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
async def test_auth_chain(password, token, authenticated) -> None:
    actor_uri = "http://server.test/user"
    store = MemoryResourceStore()
    actor: JSONObject = {
        "id": actor_uri,
    }
    await store.put(actor)
    credentials: JSONObject = {
        "id": "urn:uuid:1234",
        "attributedTo": actor_uri,
        "type": FIRM_NS.Credentials.value,
    }
    await store.put(credentials)
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
    request = StubHttpRequest(
        "GET", "http://server.test/", headers=headers, store=store
    )
    principal = await auth.authenticate(request)
    assert (principal is not None) == authenticated
