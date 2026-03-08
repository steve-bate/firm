import json
import re
from typing import Any

import httpx_cache
import rdflib
from pyld import jsonld

AS2 = rdflib.Namespace("https://www.w3.org/ns/activitystreams#")

JSONLD_COMPACTION_CONTEXT = {
    "@context": [
        "https://www.w3.org/ns/activitystreams",
        "https://w3c-ccg.github.io/security-vocab/contexts/security-v1.jsonld",
        {
            "@vocab": "https://www.w3.org/ns/activitystreams#",
        },
    ]
}

JSONLD_CONTEXT = {"@context": "https://www.w3.org/ns/activitystreams"}

_JSON_LD_ACCEPT = {
    "Accept": "application/ld+json, application/json;q=0.5",
}


def httpx_document_loader(url: str, options: dict[str, Any]) -> dict[str, Any]:
    if "headers" in options:
        options["headers"].update(_JSON_LD_ACCEPT)
    else:
        options["headers"] = _JSON_LD_ACCEPT
    del options["documentLoader"]
    with httpx_cache.Client(cache=httpx_cache.FileCache(cache_dir="/tmp")) as client:
        response = client.get(
            url,
            **options,
        )
        if response.is_success:
            return {
                "contextUrl": None,
                "documentUrl": url,
                "document": response.json(),
            }
        raise Exception(f"Failed to load context document from {url}")


def _insert_resource(g: rdflib.Graph, resource: dict[str, Any]) -> rdflib.URIRef:
    try:
        subject = rdflib.URIRef(resource["@id"]) if "@id" in resource else rdflib.BNode()
        for key, value in resource.items():
            if key == "@id":
                continue
            if key == "@type":
                for type_ in value:
                    g.add((subject, rdflib.RDF.type, rdflib.URIRef(type_)))
            else:
                for obj in value:
                    if len(obj) == 1:
                        if "@list" in obj:
                            if len(obj["@list"]) > 0:
                                # TODO This list should be handled differently
                                # if it's LIFO vs FIFO
                                # AP OrderedCollection could be either depending on
                                # the specific collection
                                # items_uri = list(g.objects(subject, AS2.orderedItems))
                                # first_item = None
                                # if len(items_uri) == 0:
                                #     items_uri = rdflib.BNode()
                                #     g.add((subject, AS2.orderedItems, items_uri))
                                # else:
                                #     items_uri = items_uri[0]
                                #     first_item = next(g.objects((items_uri,
                                #         rdflib.RDF.first)))
                                # c = rdflib.collection.Collection(g, items_uri)
                                # for item in obj["@list"]:
                                #     item_uri = item["@id"]
                                raise Exception("@list not supported")
                            else:
                                continue
                        if "@id" in obj:
                            obj = rdflib.URIRef(obj["@id"])
                        elif "@value" in obj:
                            obj = rdflib.Literal(obj["@value"])
                        g.add((subject, rdflib.URIRef(key), obj))
                    else:
                        if "@value" in obj:
                            obj = rdflib.Literal(obj["@value"])
                        else:
                            obj = _insert_resource(g, obj)
                        g.add((subject, rdflib.URIRef(key), obj))
        return subject
    except:
        print(json.dumps(resource, indent=2))
        raise


def jsonld_to_graph(doc: dict[str, Any]) -> rdflib.Graph:
    expanded = jsonld.expand(
        doc,
        {"documentLoader": httpx_document_loader},
    )
    g = rdflib.Graph()
    for resource in expanded:
        _insert_resource(g, resource)
    return g


def node_to_python(obj: rdflib.term.Node) -> Any:
    if isinstance(obj, rdflib.BNode):
        return f"_:{obj}"
    if isinstance(obj, rdflib.Literal):
        return obj.toPython()
    if isinstance(obj, rdflib.URIRef):
        return {"@id": str(obj)}
    return str(obj)


def subject_to_jsonld(g: rdflib.Graph, uri: str) -> dict[str, Any] | None:
    resource = g.cbd(rdflib.URIRef(uri))
    jsonld_resources = []
    for subject in resource.subjects(unique=True):
        # TODO Review this blank node hack
        if True or not isinstance(subject, rdflib.BNode):
            subject_doc = {"@id": str(subject)}
            for p, o in resource.predicate_objects(subject):
                if p == rdflib.RDF.type:
                    subject_doc["@type"] = o
                else:
                    subject_doc[str(p)] = node_to_python(o)
            jsonld_resources.append(subject_doc)
    if len(jsonld_resources) == 0:
        return None
    jsonld_graph = {"@graph": jsonld_resources}
    compacted: dict[str, Any] = jsonld.compact(
        jsonld_graph,
        JSONLD_COMPACTION_CONTEXT,
        dict(
            documentLoader=httpx_document_loader,
        ),
    )
    compacted.update(JSONLD_COMPACTION_CONTEXT)
    # Sadly, the pyld library doesn't fully compact the as types.
    if subject_type := compacted.get("type"):
        if isinstance(subject_type, list):
            compacted["type"] = [re.sub("^as:", "", t) for t in subject_type]
        else:
            compacted["type"] = re.sub("^as:", "", subject_type)
    return compacted
