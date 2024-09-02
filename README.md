# FIRM: Federated Information Resource Manager

> It's not SoLiD, but it's FIRM.

This is an experimental [ActivityPub](https://www.w3.org/TR/activitypub/)-enabled federated information resource manager. This library can be used to implement ActivityPub, [Linked Data](https://en.wikipedia.org/wiki/Linked_data), and similar servers. It's currently not intended to be the basis for a public servers, but rather a platform for experimentation and implementing proof-of-concept for social web ideas.

This library is being used to implement the `firm-server`, which added network access, simple web interfaces and other features. The `firm-server` is just an example, other servers can be build with this library. Those servers can use a different web framework, different storage strategies, and os on.

This library is still in the early stages of development, but there are already some potentially useful and interesting features:

## Features

These are current features, unless tagged otherwise.

- Python 3 implementation (libraries for other languages being developed)
  - Minimal external dependencies (only cryptography libraries for HTTP signatures)
- Multi-actor
- Multitenant
    - Multiple domains supported on a single server
- Data vocabulary-independent
  - Not specific to [ActivityStreams 2.0](https://www.w3.org/TR/activitystreams-vocabulary/)
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
    - [HTTP Basic Auth](https://developer.mozilla.org/en-US/docs/Web/HTTP/Authentication)
    - Bearer Tokens
    - Can be used simultaneously (chained)
    - #future [OAuth2](https://oauth.net/2/)
    - #future [FEP](https://codeberg.org/fediverse/fep)-specified schemes, new [RFC 9421 HTTP Signatures](https://datatracker.ietf.org/doc/rfc9421/), etc.
- Partial ActivityPub S2S implementation
  - Implements [activitypub-mincore](https://github.com/steve-bate/activitypub-mincore) and more.
  - Interoperates with Mastodon Follow, Undo and Create activities.
- Partial ActivityPub C2S implementation
- Extensible WebFinger (Resource-specific properties)
  - Interoperates with Mastodon
- Extensible NodeInfo (Tenant-specific Metadata)


## Road Map

*This is very subject to change.*

- Version 0.1.1
  - Integration testing with [activitypub-testsuite](https://github.com/steve-bate/activitypub-testsuite) (and/or [feditest](https://feditest.org/))
- Version 0.2.0
    - RDF Graph Storage (mostly implemented already)
    - SPARQL endpoint (already implemented)
    - Full-Text Search on RDF data (implemented already)
- Version 0.3.0
    - JSON Schema Validation ([fediverse-json-schema](https://github.com/steve-bate/fediverse-jsonschema))
    - ActivityPub [Media Upload](https://www.w3.org/wiki/SocialCG/ActivityPub/MediaUpload)
    - ActivityPub C2S Proxy Endpoint
- Version 0.4.0
    - [SoLiD-lite](https://solid-lite.org/) support
    - Document management
- Long Term
    - Event streaming
    - ActivityPub C2S Extensions
    - Mastodon-compatible UI API
    - Additional storage implementations (see above)
    - FEP prototyping and proof-of-concept demonstrations
    - Domain-specific server implementations
