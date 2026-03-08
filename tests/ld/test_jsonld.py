import json

from pyld import jsonld

from firm.ld.jsonld_utils import (
    httpx_document_loader,
    jsonld_to_graph,
    subject_to_jsonld,
)


def test_expand_jsonld():
    doc = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": "https://server.test/activity",
        "type": "Create",
        "actor": "https://server.test/actor",
        "object": {
            "type": "Note",
            "content": "Hello, world!",
        },
    }
    jsonld.expand(
        doc,
        {"documentLoader": httpx_document_loader},
    )
    # smoke test


def test_graph_insert():
    doc = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": "https://server.test/activity",
        "type": "Create",
        "actor": "https://server.test/actor",
        "object": {
            "type": "Note",
            "content": "Hello, world!",
        },
    }
    g = jsonld_to_graph(doc)
    print(g.serialize())
    assert (
        g.serialize().strip()
        == """
@prefix ns1: <https://www.w3.org/ns/activitystreams#> .

<https://server.test/activity> a ns1:Create ;
    ns1:actor <https://server.test/actor> ;
    ns1:object [ a ns1:Note ;
            ns1:content "Hello, world!" ] .""".strip()
    )


def test_graph_extract():
    doc = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": "https://server.test/activity",
        "type": "Create",
        "actor": "https://server.test/actor",
        "object": {
            "id": "https://server.test/object",
            "type": "Note",
            "content": "Hello, world!",
        },
    }
    g = jsonld_to_graph(doc)
    # Graph is setup
    jsonld_doc = subject_to_jsonld(g, str(doc["id"]))
    print(json.dumps(jsonld_doc, indent=2))
