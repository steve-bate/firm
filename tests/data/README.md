# Bundled JSON-LD contexts

Copied from their canonical sources so the RDF tests never need the network. `pyld` fetches
`@context` documents at runtime; without these, every test that compacts or expands trips a
Cloudflare challenge on w3.org and fails for reasons unrelated to the code.

| file | source | copied |
| --- | --- | --- |
| `activitystreams.jsonld` | `https://raw.githubusercontent.com/w3c/activitystreams/main/ns/activitystreams.jsonld` — the source of `https://www.w3.org/ns/activitystreams` | 2026-10-29 |
| `security-v1.jsonld` | `https://w3c-ccg.github.io/security-vocab/contexts/security-v1.jsonld` | 2026-10-29 |

`tests/ld/conftest.py` serves them to `firm.ld.jsonld_utils.httpx_document_loader` and fails
the test if anything else is fetched, so a new network dependency shows up as an error
rather than as a flaky pass. Refresh a file by re-fetching its source; do not hand-edit.
