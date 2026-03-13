# FIRM: Federated Information Resource Manager

> It's not SoLiD, but it's FIRM.

This is an experimental [ActivityPub](https://www.w3.org/TR/activitypub/)-enabled federated information resource manager. This library can be used to implement ActivityPub, [Linked Data](https://en.wikipedia.org/wiki/Linked_data), and similar servers. It's currently not intended to be the basis for a public servers, but rather a platform for experimentation and implementing proof-of-concept for social web ideas.

>[!WARNING]
This library is still in the early stages of development, but there are already some potentially useful and interesting features:

## Core Features

These are current features, unless tagged otherwise.

- Python 3 implementation (libraries for other languages being developed)
  - Minimal external dependencies (only cryptography libraries for HTTP signatures)
- Multi-actor
- Multitenant
    - Multiple domains supported on a single server
- Data vocabulary-independent
  - Not specific to [ActivityStreams 2.0](https://www.w3.org/TR/activitystreams-vocabulary/)
- Flexible URIs (supports any HTTP URI path structure)
- Abstract web interface
- Abstract resource store
  - Flexible data partitioning
  - Multiple storage implementations (can be used together)
    - In-Memory
    - File System (JSON)
    - [Sqlite3](https://sqlite.org/json1.html) (JSON)
    - #future [deltabase](https://github.com/uname-n/deltabase)
    - #future [MongoDB](https://www.mongodb.com/)
    - #future [RDF Graph Store](https://rdflib.readthedocs.io/en/stable/index.html)
    - #future [Neo4J](https://neo4j.com/)
    - #future File System + [git](https://git-scm.com/)
- Multiple authentication techniques
    - HTTP Signatures ([Cavage](https://datatracker.ietf.org/doc/html/draft-cavage-http-signatures-12))
    - Client Authentication/Authorization
      - [OAuth2](https://oauth.net/2/)
      - [HTTP Basic Auth](https://developer.mozilla.org/en-US/docs/Web/HTTP/Authentication)
      - Bearer Tokens
    - Methods can be used simultaneously (chained)
    - #future new [RFC 9421 HTTP Signatures](https://datatracker.ietf.org/doc/rfc9421/), etc.
- Implements both S2S and C2S ActivityPub profiles.
- Extensible WebFinger (Resource-specific properties)
  - Interoperates with Mastodon
- Extensible NodeInfo (Tenant-specific Metadata)
- [JSON Schema](https://json-schema.org/)-based validation (WIP)
- Event streaming Support (SSE)
- Collection filtering using [JSONPath](https://www.rfc-editor.org/rfc/rfc9535) (RFC 9535)

## Server

* Advanced Features
  * Multi-actor
  * Multi-tenant (multiple instances hosted in same server process with shared remote cache)
* File-based storage (JSON)
  * Partitioned storage
    * Remote cache, separate from tenant documents
    * Private storage
* Linked Data Support (using [firm-ld](https://github/steve-bate/firm-ld) library)
  * FIRM storage implementation that uses a RDF graph store ([rdflib](https://rdflib.readthedocs.io/en/stable/index.html), [oxigraph](https://github.com/oxigraph/oxigraph))
  * SPARQL endpoint integration (using [rdflib_endpoint](https://github.com/vemonet/rdflib-endpoint))
  * Full-text search (FTS) over selected triples ([sqlite3 FTS](https://sqlite.org/fts5.html))
* ActivityPub C2S Proxy Endpoint
* Uses [FastAPI](https://fastapi.tiangolo.com/) and [uvicorn](https://www.uvicorn.org/)
* Allows per-tenant customization
* Integration testing with [FIRM testsuite](https://github.com/steve-bate/firm-testsuite) based on the [activitypub-testsuite](https://github.com/steve-bate/activitypub-testsuite)

## Road Map

*This is very subject to change.*

- Version 0.2.0
  - Admin Web Interface
  - ActivityPub [Media Upload](https://www.w3.org/wiki/SocialCG/ActivityPub/MediaUpload)
- Version 0.3.0
  - RDF Graph Storage (mostly implemented already)
  - SPARQL endpoint (already implemented)
  - Full-Text Search on RDF data (implemented already)
- Long Term
  - More ActivityPub C2S Extensions
  - Additional storage implementations (see above)
  - FEP prototyping and proof-of-concept demonstrations
  - Domain-specific server implementations based on FIRM foundation.
