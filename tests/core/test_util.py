import re

import pytest

from firm.core.interfaces import Tenant
from firm.core.store.memory import MemoryResourceStore
from firm.core.util import get_version, is_local_uri, is_urn


@pytest.fixture
def tenant(tmp_path) -> Tenant:
    return Tenant("http://tenant1.test", MemoryResourceStore(), MemoryResourceStore(), tmp_path)


def test_get_version():
    assert re.match(r"\d+\.\d+\.\d+", get_version("firm"))


def test_is_local_uri(tenant):
    assert is_local_uri(tenant, "http://tenant1.test/obj1")
    assert is_local_uri(tenant, "urn:uuid:3f6b1c8e-0f1a-4b2e-9c3d-2a7e5f8b9d01")

    assert not is_local_uri(tenant, "http://tenant2.test/obj1")
    assert not is_local_uri(tenant, "https://remote.test/note/1")


def test_is_urn():
    assert is_urn("urn:uuid:3f6b1c8e-0f1a-4b2e-9c3d-2a7e5f8b9d01")
    assert is_urn("urn:example:whatever")

    assert not is_urn("http://tenant1.test/obj1")
    assert not is_urn("https://remote.test/urn:not-really")
