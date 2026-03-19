import jsonpath_rfc9535 as jsonpath

from firm.core.interfaces import JSON, JSONObject
from firm.core.services.activitypub.service import item_filter
from firm.core.util import get_collection_items


def collection_filter(
    expr: str, resource: JSONObject, offset: int | None = None, limit: int | None = None
) -> list[JSON]:
    items = get_collection_items(resource)
    if not items:
        return []
    expr = item_filter(expr)
    return [node.value for node in jsonpath.find(expr, items)]


COLLECTION = {
    "@context": "https://www.w3.org/ns/activitystreams",
    "id": "https://example.social/users/alice/outbox",
    "type": "OrderedCollection",
    "totalItems": 11,
    "orderedItems": [
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": "https://example.social/activities/1",
            "type": "Create",
            "actor": "https://example.social/users/alice",
            "published": "2026-02-20T08:00:00Z",
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
            "object": {
                "id": "https://example.social/objects/note-1",
                "type": "Note",
                "content": "Hello fediverse!",
                "published": "2026-02-20T08:00:00Z",
                "attachment": [
                    {
                        "type": "Image",
                        "mediaType": "image/jpeg",
                        "url": "https://cdn.example.social/media/photo-1.jpg",
                        "name": "Sunrise over the city",
                    },
                    {
                        "type": "Document",
                        "mediaType": "application/pdf",
                        "url": "https://cdn.example.social/media/hello-fediverse.pdf",
                        "name": "Post transcript",
                    },
                ],
            },
        },
        {
            "@context": [
                "https://www.w3.org/ns/activitystreams",
                "https://w3id.org/security/v1",
            ],
            "id": "https://example.social/activities/2",
            "type": "Like",
            "actor": "https://example.social/users/bob",
            "published": "2026-02-20T08:05:00Z",
            "object": "https://example.social/objects/note-1",
            "to": ["https://example.social/users/alice"],
        },
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": "https://example.social/activities/3",
            "type": "Follow",
            "actor": "https://example.social/users/carla",
            "published": "2026-02-20T08:10:00Z",
            "object": "https://example.social/users/alice",
            "cc": ["https://www.w3.org/ns/activitystreams#Public"],
        },
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": "https://example.social/activities/4",
            "type": "Accept",
            "actor": "https://example.social/users/alice",
            "published": "2026-02-20T08:12:00Z",
            "object": {
                "id": "https://example.social/activities/3",
                "type": "Follow",
                "actor": "https://example.social/users/carla",
                "object": "https://example.social/users/alice",
            },
            "to": ["https://example.social/users/carla"],
        },
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": "https://example.social/activities/5",
            "type": "Announce",
            "actor": "https://example.social/users/dan",
            "published": "2026-02-20T08:20:00Z",
            "object": "https://example.social/objects/note-1",
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
            "cc": ["https://example.social/users/alice/followers"],
        },
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": "https://example.social/activities/6",
            "type": "Delete",
            "actor": "https://example.social/users/erin",
            "published": "2026-02-20T10:15:00Z",
            "object": {
                "id": "https://example.social/objects/note-99",
                "type": "Tombstone",
                "formerType": "Note",
                "deleted": "2026-02-20T10:15:00Z",
            },
            "to": ["https://example.social/users/erin/followers"],
        },
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": "https://example.social/activities/7",
            "type": "Update",
            "actor": "https://example.social/users/alice",
            "published": "2026-02-20T10:30:00Z",
            "object": {
                "id": "https://example.social/users/alice",
                "type": "Person",
                "name": "Alice A.",
                "summary": "Building federated apps.",
                "attachment": [
                    {
                        "type": "Image",
                        "mediaType": "image/png",
                        "url": "https://cdn.example.social/media/alice-banner.png",
                        "name": "Profile banner",
                    }
                ],
            },
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
        },
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": "https://example.social/activities/8",
            "type": "Undo",
            "actor": "https://example.social/users/bob",
            "published": "2026-02-20T10:40:00Z",
            "object": {
                "id": "https://example.social/activities/2",
                "type": "Like",
                "object": "https://example.social/objects/note-1",
            },
            "to": ["https://example.social/users/alice"],
        },
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": "https://example.social/activities/9",
            "type": "Block",
            "actor": "https://example.social/users/frank",
            "published": "2026-02-20T10:50:00Z",
            "object": "https://example.social/users/spam-bot",
            "to": ["https://example.social/users/frank"],
        },
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": "https://example.social/activities/10",
            "type": "Add",
            "actor": "https://example.social/users/alice",
            "published": "2026-02-20T11:00:00Z",
            "object": "https://example.social/objects/note-1",
            "target": "https://example.social/users/alice/collections/featured",
            "to": ["https://example.social/users/alice"],
        },
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": "https://example.social/activities/11",
            "type": "Flag",
            "actor": "https://example.social/users/grace",
            "published": "2026-02-20T11:10:00Z",
            "object": [
                {
                    "id": "https://example.social/objects/note-44",
                    "type": "Note",
                    "attachment": [
                        {
                            "type": "Video",
                            "mediaType": "video/mp4",
                            "url": "https://cdn.example.social/media/evidence-clip.mp4",
                            "name": "Harassment evidence clip",
                        }
                    ],
                },
                "https://example.social/users/spam-bot",
            ],
            "content": "Spam and harassment report",
            "to": ["https://example.social/moderators"],
        },
    ],
}


def test_get_crud():
    for value in collection_filter(
        "@.type == 'Create' || @.type == 'Update' || @.type == 'Delete'", COLLECTION
    ):
        assert value["type"] in ["Create", "Update", "Delete"]


def test_get_non_crud():
    for value in collection_filter(
        "!(@.type == 'Create' || @.type == 'Update' || @.type == 'Delete')", COLLECTION
    ):
        assert value["type"] not in ["Create", "Update", "Delete"]


def test_get_actors_objects():
    for value in collection_filter("@.actor == 'https://example.social/users/alice'", COLLECTION):
        assert value["actor"] == "https://example.social/users/alice"


def test_date_query():
    for value in collection_filter("@.published <= '2026-02-20T10:30:00Z'", COLLECTION):
        assert value["published"] <= "2026-02-20T10:30::00Z'"


def test_get_objects_with_video_attachment():
    object_ids = collection_filter(
        "[*].object[?@.attachment[?@.type == 'Video']].id",
        COLLECTION,
    )
    assert [value for value in object_ids] == ["https://example.social/objects/note-44"]


def test_get_activities_related_to_an_object():
    activity_ids = collection_filter(
        "[?@.object == 'https://example.social/objects/note-1' || @..object[?@ == 'https://example.social/objects/note-1']].id",
        COLLECTION,
    )

    values = set(activity_ids)

    assert values == {
        "https://example.social/activities/1",
        "https://example.social/activities/2",
        "https://example.social/activities/5",
        "https://example.social/activities/8",
        "https://example.social/activities/10",
    }


def test_extension_match():
    values = set(collection_filter("[?match(@.actor, '.*bob.*')].id", COLLECTION))
    assert values == {
        "https://example.social/activities/2",
        "https://example.social/activities/8",
    }


def test_extension_search():
    values = set(collection_filter("[?search(@.actor, '[Bb]ob')].id", COLLECTION))
    assert values == {
        "https://example.social/activities/2",
        "https://example.social/activities/8",
    }


#
# https://codeberg.org/fediverse/fep/src/branch/main/fep/6606/fep-6606.md
#


class TestFep6606Compatibility:

    # ?element=value
    # ... ?type=Create
    # // resources matching exactly "value"
    def test_simple_equality(self):
        values = collection_filter("@.type == 'Create'", COLLECTION)
        for value in values:
            assert value["type"] == "Create"

    # ?element=1&element=2
    # ... ?type=Create&type=Update
    # // resources matching exactly "1" or "2"
    def test_or(self):
        values = collection_filter("@.type == 'Create' || @.type == 'Update'", COLLECTION)
        for value in values:
            assert value["type"] in ["Create", "Update"]

    # ?element=!value1
    # ... ?type=!Delete
    # // resources inversly matching "value1"
    def test_not_delete(self):
        values = collection_filter("@.type != 'Delete'", COLLECTION)
        for value in values:
            assert value["type"] != "Delete"

    # ?element=!1&element=!2
    # ... ?type=!Create&type=!Update
    # // resources inversly matching "1" and "2"
    def test_not_or(self):
        values = collection_filter("@.type != 'Create' && @.type != 'Update'", COLLECTION)
        for value in values:
            assert value["type"] not in ["Create", "Update"]

    # ?element=~fuzzy
    # ... ?type=~Cre
    # // resources fuzzy matching "fuzzy"
    def test_fuzzy_match(self):
        values = collection_filter("search(@.type, '.*Cre.*')", COLLECTION)
        for value in values:
            assert "Cre" in value["type"]

    # ?element=~one&element=~two
    # ... ?type=~Cre&type=~Up
    # // resources fuzzy matching "one" or "two"
    def test_fuzzy_or(self):
        values = collection_filter(
            "search(@.type, '.*Cre.*') || search(@.type, '.*Up.*')", COLLECTION
        )
        for value in values:
            assert "Cre" in value["type"] or "Up" in value["type"]

    # ?element=-
    # ... ?bogus=-
    # // resources matching empty element values
    def test_property_value_doesnt_exist(self):
        values = collection_filter("[?!@.bogus]", COLLECTION)
        for value in values:
            assert value.get("bogus") is None

    # ?element=!-
    # ... ?content=!-
    # // resources matching all non empty element values
    def test_property_value_exists(self):
        values = collection_filter("[?@.content]", COLLECTION)
        for value in values:
            assert value.get("content") is not None
