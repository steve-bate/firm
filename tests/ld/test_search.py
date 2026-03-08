import json

import pytest
from rdflib import RDF, RDFS, Graph, Literal, URIRef

from firm.ld.search import IndexedResource, SearchEngine


@pytest.fixture
def graph():
    graph = Graph()

    def subject(n):
        return URIRef(f"http://server.test/subject-{n}")

    for triple in [
        (subject(1), RDF.type, URIRef("Foo")),
        (subject(1), RDFS.label, Literal("My label")),
        (subject(2), RDF.type, URIRef("Foo")),
        (subject(2), RDFS.label, Literal("Something else")),
        (subject(3), RDF.type, URIRef("Bar")),
        (subject(3), RDFS.label, Literal("Another label")),
    ]:
        graph.add(triple)
    return graph


def test_search(graph):
    engine = SearchEngine(graph)
    engine.add_index(
        IndexedResource(
            type=URIRef("Foo"),
            indexed=[RDFS.label],
            projected=[RDFS.label],
        )
    )
    engine.add_index(
        IndexedResource(
            type=URIRef("Bar"),
            context="http://server.example/context",
            indexed=[RDFS.label],
            projected=[RDFS.label],
        )
    )
    engine.update_index()
    response = engine("label")
    print(f"{json.dumps(json.loads(response.body), indent=2)}")
