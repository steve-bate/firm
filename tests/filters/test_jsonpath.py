import jsonpath_rfc9535 as jsonpath

data = {
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
    nodes = jsonpath.find(
        "$.orderedItems[?@.type == 'Create' || @.type == 'Update' || @.type == 'Delete']", data
    )
    for node in nodes:
        assert node.value["type"] in ["Create", "Update", "Delete"]


def test_get_non_crud():
    nodes = jsonpath.find(
        "$.orderedItems[?!(@.type == 'Create' || @.type == 'Update' || @.type == 'Delete')]",
        data,
    )
    for node in nodes:
        assert node.value["type"] not in ["Create", "Update", "Delete"]


def test_get_actors_objects():
    nodes = jsonpath.find("$.orderedItems[?@.actor=='https://example.social/users/alice']", data)
    for node in nodes:
        assert node.value["actor"] == "https://example.social/users/alice"


def test_date_query():
    nodes = jsonpath.find("$.orderedItems[?@.published<='2026-02-20T10:30:00Z']", data)
    for node in nodes:
        assert node.value["published"] <= "2026-02-20T10:30:00Z"


def test_get_objects_with_video_attachment():
    video_objects = jsonpath.find(
        "$.orderedItems[*].object[?@.attachment[?@.type == 'Video']]",
        data,
    )
    object_ids = jsonpath.find("$[*].id", [node.value for node in video_objects])

    assert [node.value for node in object_ids] == ["https://example.social/objects/note-44"]


def test_get_activities_related_to_an_object():
    related_activities = jsonpath.find(
        "$.orderedItems[?@.object == 'https://example.social/objects/note-1' || @..object[?@ == 'https://example.social/objects/note-1']]",
        data,
    )
    activity_ids = jsonpath.find("$[*].id", [node.value for node in related_activities])

    results = set(node.value for node in activity_ids)

    assert results == {
        "https://example.social/activities/1",
        "https://example.social/activities/2",
        "https://example.social/activities/5",
        "https://example.social/activities/8",
        "https://example.social/activities/10",
    }
