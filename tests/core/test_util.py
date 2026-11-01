import re

import pytest

from firm.core.interfaces import Tenant
from firm.core.store.memory import MemoryResourceStore
from firm.core.util import (
    get_version,
    is_local_uri,
    is_urn,
    normalize_collection_items,
)


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


def test_normalize_renames_the_wire_key_to_items():
    document = {"id": "c", "type": "OrderedCollection", "orderedItems": ["a", "b"]}
    assert normalize_collection_items(document)["items"] == ["a", "b"]
    assert "orderedItems" not in document


def test_normalize_prefers_items_when_both_are_present():
    document = {"id": "c", "type": "Collection", "items": ["a"], "orderedItems": ["b"]}
    assert normalize_collection_items(document)["items"] == ["a"]
    assert "orderedItems" not in document


def test_normalize_leaves_a_plain_document_alone():
    document = {"id": "n", "type": "Note", "content": "hi"}
    assert normalize_collection_items(document) == {"id": "n", "type": "Note", "content": "hi"}
