import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence, cast

import rdflib
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)


def _ensure_uri(value: rdflib.URIRef | str) -> rdflib.URIRef:
    if not isinstance(value, rdflib.URIRef):
        return rdflib.URIRef(value)
    return value


@dataclass
class IndexedResource:
    type: rdflib.URIRef | str
    indexed: Sequence[rdflib.URIRef | str]
    projected: Sequence[rdflib.URIRef | str]
    context: Any = None

    def __post_init__(self):
        self.type = _ensure_uri(self.type)
        self.indexed = [_ensure_uri(pred) for pred in self.indexed]
        self.projected = [_ensure_uri(pred) for pred in self.projected]


class SearchEngine:
    """A simple full-text search engine for RDF resources that indexes text objects."""

    def __init__(self, graph: rdflib.Graph):
        self._index = sqlite3.connect(":memory:", check_same_thread=False)
        self._index.row_factory = sqlite3.Row
        self._index.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS " "resource_fts USING fts4(uri, type, text)"
        )
        self._indexed_resources: dict[rdflib.URIRef, IndexedResource] = {}
        self._graph = graph

    def add_index(self, resource_config: IndexedResource) -> None:
        self._indexed_resources[resource_config.type] = resource_config

    def _get_object(
        self, subject: rdflib.URIRef, predicate: rdflib.URIRef
    ) -> rdflib.URIRef | rdflib.Literal:
        objs = list(self._graph.objects(subject=subject, predicate=predicate))
        return objs[0] if len(objs) > 0 else None

    def update_index(self, subject: rdflib.URIRef | None = None) -> None:
        if subject is None:
            subjects = self._graph.subjects(predicate=rdflib.RDF.type, unique=True)
        else:
            subjects = [subject]

        for subject in subjects:
            resource_type = self._get_object(subject, rdflib.RDF.type)
            if config := self._indexed_resources.get(resource_type):
                text = ""
                for pred in config.indexed:
                    if objects := list(self._graph.objects(subject, pred)):
                        text += " " + ",".join(objects)
                self._index.execute(
                    "INSERT INTO resource_fts(uri, type, text) VALUES (?, ?, ?)",
                    (subject.toPython(), resource_type, text),
                )
                log.info("Indexed %s", subject)

    def _projection(self, uri: str, subject_type: str) -> dict:
        result: dict[str, Any] = {"id": uri, "type": subject_type}
        subject = _ensure_uri(uri)
        if config := self._indexed_resources.get(_ensure_uri(subject_type)):
            for pred in cast(rdflib.URIRef, config.projected):
                values: list[str] | str = [str(obj) for obj in self._graph.objects(subject, pred)]
                if len(values) == 1:
                    values = values[0]
                result[pred.fragment] = values
        return result

    def search(self, query: str) -> list[dict]:
        cursor = self._index.execute(
            "SELECT uri, type from resource_fts WHERE text MATCH ?",
            (query,),
        )
        return [self._projection(row["uri"], row["type"]) for row in cursor.fetchall()]

    def __call__(self, query: str) -> JSONResponse:
        subjects = self.search(query)
        results = []
        for subject in subjects:
            subject_uri = rdflib.URIRef(subject)
            resource_type = self._get_object(subject_uri, rdflib.RDF.type)
            if config := self._indexed_resources.get(resource_type):
                resource = {}
                if config.context:
                    resource["@context"] = config.context
                resource["id"] = subject
                for pred in cast(rdflib.URIRef, config.projected):
                    if objects := list(self._graph.objects(subject_uri, pred)):
                        key = pred.fragment or str(pred)
                        resource[key] = ",".join(objects)
                results.append(resource)
        return JSONResponse(results)
