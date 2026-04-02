# These tests are partially exploratory

import pytest
import rdflib.plugins.sparql
from rdflib import RDFS, Dataset, Literal, URIRef, Variable
from rdflib.plugins.sparql import prepareUpdate
from rdflib.plugins.sparql.parserutils import CompValue

rdflib.plugins.sparql.SPARQL_LOAD_GRAPHS = False


def pprintAlgebra(q) -> None:
    def pp(p, ind="    "):
        # if isinstance(p, list):
        #     print "[ "
        #     for x in p: pp(x,ind)
        #     print "%s ]"%ind
        #     return
        if not isinstance(p, CompValue):
            print(p)
            return
        print("%s(" % (p.name,))
        for k in p:
            print(
                "%s%s ="
                % (
                    ind,
                    k,
                ),
                end=" ",
            )
            pp(p[k], ind + "    ")
        print("%s)" % ind)

    try:
        a = q.algebra
        if isinstance(a, list):
            for item in a:
                pp(item)
        else:
            pp(q.algebra)
    except AttributeError:
        # it's update, just a list
        for x in q:
            pp(x)


def dump_results(results, label=None):
    if label:
        print(f"{label}:")
    for result in results.bindings:
        indent = "  " if label else ""
        r = [(f"?{k}", str(v)) for k, v in result.items()]
        print(f"{indent}{r}")


CONTEXT_1 = URIRef("http://server.test/test-1")
CONTEXT_2 = URIRef("http://server.test/test-2")


@pytest.fixture
def dataset_1():
    dataset = Dataset()
    subject = URIRef("http://server.test/subject")
    dataset.add((subject, RDFS.label, Literal("label-default")))
    dataset.add((subject, RDFS.label, Literal("label-2"), CONTEXT_1))
    dataset.add((subject, RDFS.label, Literal("label-3"), CONTEXT_2))
    return dataset


def dict_eq(d1, d2):
    return sorted(d1.items()) == sorted(d2.items())


def assert_results(actual_results, expected_bindings):
    for b in expected_bindings:
        assert (
            next((ab for ab in actual_results.bindings if dict_eq(b, ab)), None) is not None
        ), f"Missing binding: {b}"
    assert len(expected_bindings) == len(actual_results.bindings)


def test_default_graph_query(dataset_1):
    assert_results(
        dataset_1.query("SELECT * WHERE { ?s ?p ?o . }"),
        [
            {
                Variable("s"): URIRef("http://server.test/subject"),
                Variable("p"): RDFS.label,
                Variable("o"): Literal("label-default"),
            }
        ],
    )


def test_embedded_named_graph_var(dataset_1):
    # Only the default graph

    # Just the named graphs
    assert_results(
        dataset_1.query("SELECT * WHERE { GRAPH ?g { ?s ?p ?o . } }"),
        [
            {
                Variable("g"): CONTEXT_2,
                Variable("s"): URIRef("http://server.test/subject"),
                Variable("p"): RDFS.label,
                Variable("o"): Literal("label-3"),
            },
            {
                Variable("g"): CONTEXT_1,
                Variable("s"): URIRef("http://server.test/subject"),
                Variable("p"): RDFS.label,
                Variable("o"): Literal("label-2"),
            },
        ],
    )


def test_embedded_named_graph_specific(dataset_1):
    # A specific named graph
    assert_results(
        dataset_1.query(f"SELECT * WHERE {{ GRAPH <{CONTEXT_2}> {{ ?s ?p ?o . }} }}"),
        [
            {
                Variable("s"): URIRef("http://server.test/subject"),
                Variable("p"): RDFS.label,
                Variable("o"): Literal("label-3"),
            }
        ],
    )


@pytest.mark.skip(reason="FROM NAMED seems to be broken in rdflib")
def test_from_named(dataset_1):
    # Using FROM NAMED (seems to be broken in rdflib)
    assert_results(
        dataset_1.query(f"SELECT * FROM NAMED <{CONTEXT_2}> WHERE {{ ?s ?p ?o . }}"),
        [
            # Only binds to default graph
            # FROM NAMED is parsed but effectively ignored (bug?)
            {
                Variable("s"): URIRef("http://server.test/subject"),
                Variable("p"): RDFS.label,
                Variable("o"): Literal("label-default"),
            }
        ],
    )


def test_create_graph(dataset_1):
    update = prepareUpdate("""
# CLEAR GRAPH <http://server.test/test-3>;
DROP SILENT GRAPH <http://server.test/test-3>;
INSERT DATA {
    GRAPH <http://server.test/test-3> {
       <http://server.test/subject-2>
            <http://www.w3.org/2000/01/rdf-schema#label>
            "foobar" .
    }
}
""")
    pprintAlgebra(update)
    dataset_1.update(update)
    print(dataset_1.get_context(URIRef("http://server.test/test-3")).serialize())
