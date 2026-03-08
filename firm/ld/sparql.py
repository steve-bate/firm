from rdflib_endpoint import SparqlEndpoint

from firm.ld.store import RdfDataSet


def create_sparql_endpoint(public_url: str, **kwargs) -> SparqlEndpoint:
    dataset = RdfDataSet.VALUE
    if dataset is None:
        raise Exception("Dataset is not initialize")
    return SparqlEndpoint(
        graph=dataset,
        description="A SPARQL endpoint for serving pod data.",
        enable_update=True,
        public_url=public_url,
        **kwargs,
    )
