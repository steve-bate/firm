import logging
from typing import cast

import rdflib
from pyld import jsonld

from firm.core.interfaces import JSONObject, QueryCriteria, ResourceStore
from firm.core.store.base import ResourceStoreBase
from firm.ld.jsonld_utils import (
    JSONLD_CONTEXT,
    httpx_document_loader,
    jsonld_to_graph,
    subject_to_jsonld,
)

# This store must assume that the objects being provided are
# JSON-LD. If there is no @context, the store will add a default
# AS2, Security (and toot?) context prior when expanding the
# object. The expanded object is used for managing the storage.

log = logging.getLogger(__name__)


class RdfDataSet:
    VALUE: rdflib.Graph | None = None

    @classmethod
    def configure(cls, store_name: str, store_args: list[str]) -> None:
        dataset = rdflib.Dataset(store=store_name)
        result = dataset.open(*store_args)
        # TODO Provide more specific error handling
        if result != 1:
            raise Exception("Store open error")
        cls.VALUE = dataset

    @classmethod
    def close(cls) -> None:
        if cls.VALUE:
            cls.VALUE.close()
        cls.VALUE = None


class RdfResourceStore(ResourceStoreBase, ResourceStore):
    def __init__(self, graph: str | rdflib.Graph | None = None) -> None:
        if isinstance(graph, rdflib.Dataset):
            # TODO support named graphs
            self.graph = graph.default_context
        elif isinstance(graph, rdflib.Graph):
            self.graph = graph
        elif isinstance(graph, str):
            if RdfDataSet.VALUE is None:
                raise Exception("No RDF dataset configured")
            self.graph = RdfDataSet.VALUE.graph(graph)
        else:
            raise Exception(f"Incorrect graph type {type(graph)}")

    async def get(self, uri: str) -> JSONObject | None:
        """Retrieve Object based on uri"""
        return subject_to_jsonld(self.graph, uri)

    async def is_stored(self, uri: str) -> bool:
        return (rdflib.URIRef(uri), None, None) in self.graph

    async def put(self, obj: JSONObject) -> None:
        """Store an AP Object"""
        # Should replace existing triples (maybe patch?)
        if "@context" not in obj:
            obj.update(JSONLD_CONTEXT)
        # TODO Review the context setup
        obj["@context"] = [
            obj["@context"],
            "https://w3c-ccg.github.io/security-vocab/contexts/security-v1.jsonld",
            {"firm": "https://firm.stevebate.dev#"},
        ]
        resource = jsonld_to_graph(cast(dict, obj))
        for subject in resource.subjects():
            self.graph.remove((subject, None, None))
        self.graph += resource

    async def remove(self, uri: str) -> None:
        """Remove an object from the store"""
        subject = rdflib.URIRef(uri)
        self.graph.remove((subject, None, None))

    async def query(self, criteria: QueryCriteria) -> list[JSONObject]:
        # TODO Make prefixes configurable
        query = """
PREFIX firm: <https://firm.stevebate.dev#>
Select ?subject
Where {
"""
        expanded_criteria = jsonld.expand(
            criteria,
            dict(
                expandContext=JSONLD_CONTEXT["@context"],
                documentLoader=httpx_document_loader,
            ),
        )
        for pred, (obj,) in expanded_criteria[0].items():
            if "@value" in obj:
                if isinstance(obj["@value"], str):
                    value = obj["@value"]
                    obj = f'"{value}"'
            elif "@id" in obj:
                obj = f"<{obj['@id']}>"
            if pred == "@type":
                pred = rdflib.RDF.type
            query += f"  ?subject {pred if pred.startswith("firm:") else f'<{pred}>'} {obj} .\n"
        query += "}"
        matches: list[JSONObject] = []
        for result in self.graph.query(query):
            match = await self.get(str(result[0]))
            if match:
                matches.append(match)
        return matches

    async def close(self) -> None:
        RdfDataSet.close()
