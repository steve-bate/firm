import re

import pytest

from firm.core.interfaces import Tenant
from firm.core.store.memory import MemoryResourceStore
from firm.core.util import (
    get_version,
    is_local_uri,
    is_urn,
    normalize_collection_items,
    strip_firm_properties,
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


def test_strip_firm_properties_recurses_and_keeps_every_other_key():
    document = {
        "id": "http://tenant1.test/activities/1",
        "type": "Create",
        "@context": ["https://www.w3.org/ns/activitystreams"],
        "to": ["http://tenant1.test/user1"],
        "firm:role": "admin",
        "toot:discoverable": True,
        "object": {
            "id": "http://tenant1.test/notes/1",
            "type": "Note",
            "firm:privateKey": "leak",
            "_misskey_quote": "http://remote.test/notes/1",
        },
        "items": [
            {"id": "http://tenant1.test/notes/2", "firm:collections": "http://tenant1.test/c"}
        ],
    }

    stripped = strip_firm_properties(document, lambda key: key.startswith("firm:"))

    assert "firm:role" not in stripped
    assert stripped["id"] == "http://tenant1.test/activities/1"
    assert stripped["@context"] == ["https://www.w3.org/ns/activitystreams"]
    assert stripped["to"] == ["http://tenant1.test/user1"]
    assert stripped["toot:discoverable"] is True
    assert "firm:privateKey" not in stripped["object"]
    assert stripped["object"]["_misskey_quote"] == "http://remote.test/notes/1"
    assert "firm:collections" not in stripped["items"][0]
    assert stripped["items"][0]["id"] == "http://tenant1.test/notes/2"
    assert document["firm:role"] == "admin", "the caller's document is untouched"
