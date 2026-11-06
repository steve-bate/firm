# Bookmarks

How bookmark data is modelled across the FIRM stack, reconstructed from the testbed
corpus, the store builder, this server, and the `flowz` C2S client. This is a description
of the design **as it exists**, not of an intended one: the layers were built at different
times and disagree with each other in ways recorded below.

Evidence is cited as `path:line` and as counts taken from the stores on disk. Claims I
verified by running code are marked *(verified)*; claims inferred from reading code are
marked *(inferred)*.

## Summary

Bookmarking is not a server feature. The server has no bookmark code: `grep -rni bookmark firm/`
matches exactly one line, a Font Awesome icon in `firm/server/html/templates/partials/post.jinja2:42`.
Bookmarks exist as an *instance* of the generic actor-owned-collection mechanism, produced by
a corpus generator and consumed by the C2S client:

| Layer | Location | State |
| --- | --- | --- |
| Authoring corpus | `firm-tools/social-data/*/actor.json` | 11 actors, 37 bookmark entries |
| Store builder | `firm_tools/resources/build.py::setup_bookmarks` | Writes one collection per actor, sets `actor["collections"]` |
| Stored data | `firm-data.bak` tenant `firm.stevebate.dev` | 11 `firm:Bookmarks` collections, 31 items |
| Live store | `firm-data` | 0 bookmark documents (rebuilt from the `two-tenants` scenario, which has none) |
| Server | `firm/` | No bookmark-specific code; generic collection read, generic C2S `Add`/`Remove` |
| Client | `flowz` | Bookmarks view, discovery predicate, `Remove` write; no `Add` write |

The vocabulary term `firm:Bookmarks` is declared in no code: it is a data convention,
defined by the builder and matched as a magic string by the client. Its only formal
declaration is the context file below.

## The firm namespace

The namespace is `https://firm.core.stevebate.dev/ns#` (`FIRM_NS.PREFIX`,
`firm/core/interfaces.py:33`). Its JSON-LD context is `firm/ld/contexts/firm.jsonld`: it
declares the `firm` prefix, the class terms (`Tenant`, `NodeInfo`, `WebFinger`,
`Credentials`, `OAuth2Token`, `Blocks`, `Bookmarks`) and the property terms
(`collections`, `privateKey`, `password`, `token`, `role`, `blockedActor`, `blockedDomain`,
`blockedSubnet`).

Two things follow from it:

- `firm:Bookmarks` expands to `https://firm.core.stevebate.dev/ns#Bookmarks` — the type is
  now declared in a namespace rather than being a bare string convention. *(verified:
  expanding a stored bookmark collection with `pyld` against this context and the AS2
  context.)*
- The bare `collections` key in the actor document and the `firm:collections` term the
  server writes are declared as the same property. That settles the *name*; it does not
  settle the shape (array of collection URIs vs one index URI), which is still an open
  question below.

`@type: "@id"` coercion on `collections` applies only to a document that spells the key
`collections`; a document that writes the compact IRI `firm:collections` gets the same
property but no coercion, because JSON-LD coercion is a property of the term, not of the
IRI. Nothing serves this context at the namespace URL yet — the file is the source of truth
until it is published.

## The stored shape

A bookmark set is one `Collection` document in the tenant's public store, at
`{actor-id}/bookmarks` (`firm-data.bak/tenants/firm.stevebate.dev/public/f0f987….json`):

```json
{
  "id": "https://firm.stevebate.dev/actors/kristinward/bookmarks",
  "type": ["Collection", "firm:Bookmarks"],
  "attributedTo": "https://firm.stevebate.dev/actors/kristinward",
  "totalItems": 4,
  "items": [
    {
      "type": "Link",
      "mediaType": "application/activity+json",
      "href": "https://firm.stevebate.dev/actors/brianna24/note/0e22de00-…",
      "published": "2024-07-03T15:14:00.339847+00:00"
    },
    {
      "type": "Link",
      "mediaType": "application/activity+json",
      "href": "https://firm.stevebate.dev/actors/williamschneider/note/ad769ba7-…",
      "published": "2025-03-07T15:14:00.339847+00:00",
      "summary": "This is a great read!"
    }
  ]
}
```

Three properties of this shape matter downstream:

- **Items are `Link` objects, not URIs and not embedded objects.** `href` is the bookmarked
  note, `published` is when it was bookmarked (not when the note was published), and
  `summary` carries the bookmarker's annotation. `Link` is not used for `liked`/`shares`,
  which hold bare actor/object URIs.
- **`Collection`, not `OrderedCollection`.** So ordering is not claimed, and the server's
  paging treats the newest-first/oldest-first distinction as irrelevant here.
- **No recipient fields.** There is no `to`/`cc`/`audience`, so `is_public()` is false
  (`firm/core/util.py:109`) and authorization falls through to
  `is_attributed_user` (`firm/core/auth/authorization.py:29`). Anonymous reads get 401,
  another actor gets 403, the owner gets the document. Bookmarks are owner-only by
  construction — which happens to match how Mastodon treats them, though nothing in the
  design names that intent.

The actor document references the set by URI in a plain `collections` array
(`…/public/880c6eb0….json:71`):

```json
"collections": [
  "https://firm.stevebate.dev/actors/wongbilly/bookmarks"
]
```

All 11 actor documents in `firm-data.bak` carry this key. `collections` is not an
ActivityStreams term and is not declared in the document's `@context`
(`["https://www.w3.org/ns/activitystreams", "https://w3id.org/security/v1"]`).

## How the data is produced

### 1. Corpus (authoring)

Each actor's `actor.json` in the social-data corpus carries a private working form of the
list — post handles rather than URIs (`firm-tools/scripts/add_bookmarks.py:42`):

```json
"bookmarks": [
  {"post_id": "brianna24/c736e5750da64ea3b8d6a4ec53f07485",
   "published": "2024-11-21T15:14:00.339847+00:00",
   "comment": "I always enjoy your recommendations, keep them coming!"}
]
```

`add_bookmarks.py` fills this in: for each actor, sample 1–5 posts by *other* actors that
were published at or after the actor's own `published` date, timestamp each bookmark at
`actor.published + randint(0, 360) days`, and attach one of 13 canned comments with
probability `randint(0,100) > 80` (≈19%). It is a standalone script, not part of the normal
corpus generation pipeline. `scripts/fix_post_ids.py:71` remaps `post_id` when note ids are
renumbered.

The corpus on disk holds 37 entries across the 11 actors.

### 2. Store builder

`setup_bookmarks` (`firm-tools/firm_tools/resources/build.py:130`) walks each actor's
corpus entries, resolves `post_id` → activity → object, and writes:

- the `Link` object — `href` = the stored object's `id`, `published` = bookmark timestamp,
  `summary` = the comment when present;
- the collection document shown above;
- `actor["collections"] = [collection_id]`, then re-puts the actor.

`setup_bookmarks` is the **last** step of `async_main` (`build.py:722`), after
`setup_deletes`. Post deletion therefore runs before bookmarking, and the builder happily
bookmarks a URI that now holds a `Tombstone`.

### 3. Stored result

In `firm-data.bak`, tenant `firm.stevebate.dev` (678 documents, 11 actors):

| Actor | Items |
| --- | --- |
| sheltontravis, williamschneider | 5 each |
| kristinward, anthony16, sheilaconway | 4 each |
| brianna24, laura91, wongbilly | 2 each |
| ithomas, eric80, osnyder | 1 each |

31 items in total: 24 resolve to a live `Note`, 1 to an `Image` object, and **6 to a
`Tombstone`**. One is a self-bookmark (`sheltontravis` bookmarking a `sheltontravis` note)
even though the generator excludes the actor's own posts — the `post_id` remap can move a
handle to a note by the bookmarking actor. There are no dangling hrefs: every `href` is a
document in the store.

The live `firm-data` store has none of this. It holds 6 actors (`amara`, `ines`, `joris`,
`marguerite`, `tiago`, `wouter`) built from the `two-tenants` scenario corpus, which has no
bookmark entries and whose builder path never adds a `collections` key. `firm-data.bak` is
now the only copy of the 11-actor data.

## How the server serves it

There is no bookmark route. `GET` is a catch-all (`firm/server/routes.py:498`) that resolves
any URI through `ActivityPubService.process_get` (`firm/core/services/activitypub/service.py:339`):

1. `dereference(store, uri)` finds the collection document.
2. `is_collection()` (`firm/core/util.py:243`) matches on the AS2 type — `Collection` here.
3. `_collection_response` (`service.py:422`) asks the store for the item count, serves the
   whole collection inline when it fits one page, and otherwise serves a header with
   `first`/`last`, or a page for `?page=`/`?cursor=`/`?limit=`. `?filter=` selects a JSONPath
   projection instead.
4. `serialize` (`serialization.py:71`) calls `dereference_collection_items` with
   `embed = embeds_items(collection)` (`collections.py:52`). `embeds_items` is a name
   allowlist — `followers`, `following`, `liked` are served as IRIs, everything else embeds.
   `bookmarks` embeds.
5. Items that are already dicts are passed through untouched — `Link` objects are never
   dereferenced (`collections.py:135`). Only string items are fetched, and fetch failures
   are dropped with a warning (`collections.py:119-133`).

So the intended serving behaviour is: the collection is served whole to its owner, with
`href`s intact, and a client resolves each `href` itself. That matches `flowz`, which then
fetches each bookmarked object through `getProxiedResource` in batches of 10.

## How the server registers a collection

Independently of bookmarks, this server has a second, different design for "the collections
an actor owns". Creating a collection through the outbox (`outbox.py:494`) does the
following:

- writes an index document at `{actor}/collections` with `"type": "firm:collections"`;
- sets the actor document's **`firm:collections`** property (the `FIRM_NS.collections`
  term, `firm/core/interfaces.py:47`) to that index URI;
- appends the new collection's URI to the index (`add_collection_item`).

Deleting a collection removes it from the index again (`outbox.py:457-465`). The index is a
list of collection URIs; the collections themselves are discovered by reading the index.

This is not what the bookmark data uses:

| | Bookmark data (builder) | Runtime registration (server) |
| --- | --- | --- |
| Property on actor | `collections` (plain, undeclared) | `firm:collections` |
| Value | array of collection URIs | URI of one index document |
| Index document | none | `{actor}/collections`, `firm:collections` |
| Item membership | embedded `Link` objects in `items` | URI strings in the index log |

Nothing in the server reads either property when serving — the actor document is served
as stored (`remove_empty_data` does not drop a non-empty `collections` array), and no code
path consults `actor.collections` to build a response. Discovery is entirely the client's
job, and the client implements yet a third convention.

## Write paths

**Server, C2S.** `Add` and `Remove` are processed generically for any collection
(`outbox.py:380`, `outbox.py:353`): both dereference `target` and `object`, `Add` stores the
object in the public store and inserts its URI into the target collection, `Remove` deletes
the membership. There is no check that the target is actor-owned, none that the object type
suits the collection, and `Add` copies a remote object into the tenant store rather than
recording a reference. Bookmarking through this path therefore works mechanically, but
stores a snapshot of the bookmarked note and has no annotation field to carry.

**Server, S2S.** The inbox dispatcher (`inbox.py:42-58`) handles `Follow`, `Accept`,
`Reject`, `Like`, `Create`, `Update`, `Undo`, `Delete`, `Announce`. There is no `Add` or
`Remove` handler, so a peer cannot add to a collection on this server.

**Client (`flowz`).**

- `bookmarkResource` / `unbookmarkResource` (`flowz/src/stores/timeline.ts:574`) both throw
  `Bookmark collection functionality not yet implemented`; the `Bookmark` action appears in
  the UI action list (`useResourceActions.ts:39`) but cannot succeed.
- `isBookmarked()` is implemented over the loaded collection (`timeline.ts:681`) but needs
  the discovery URI, so in practice it returns `false`; its test is labelled
  "should always return false for bookmark status (not implemented)"
  (`timeline.spec.ts:756`).
- Removal *is* wired: `Bookmarks.vue:480` builds a `Remove` activity with
  `object: <note URI>`, `target: <bookmarks collection>`, and posts it to the outbox.
- `Bookmarks.vue` reads `orderedItems || items`, accepts either `Link` objects or plain URI
  strings, and resolves each `href` individually.

## Where the layers disagree

The client finds the collection through `getBookmarksCollectionUri`
(`flowz/src/stores/resource.ts:597`), which scans `actor.collections` and calls
`isBookmarksCollection` on each entry (`resource.ts:12`):

```ts
function isBookmarksCollection(collection: any): boolean {
  if (!collection || typeof collection !== 'object') {
    return false;              // ← a URI string can never match
  }

  if (typeof collection.type === 'string') {
    return collection.type === 'firm:Bookmarks' || collection.type.includes('firm:Bookmarks');
  }

  if (!Array.isArray(collection.type)) {
    return false;
  }

  return [...collection.type].some(t => t === 'firm:Bookmarks');
}
```

Two consequences:

- The stored data holds **URI strings**, which the predicate rejects by construction. The
  client's later `typeof collection === 'string'` branch inside `getBookmarksCollectionUri`
  is dead code, only reachable after the object-only guard has already returned `false`.
- The client expects the *type* to be discoverable from the actor document, i.e. either
  inline collection objects carrying `"type": ["Collection","firm:Bookmarks"]`, or a
  `Record<string, string>` keyed `bookmarks`. The builder produces neither.

`Navigation.vue:187` gates the Bookmarks nav item on `hasBookmarksCollection`, so with real
data the link is hidden and the view is only reachable by URL.

Also note the builder comment at `build.py:160` — "Reference the collection by URI: the
store holds documents, and a document that embeds another document violates the CBD rule."
The intent there was to keep the actor document free of embedded collections, which is
right; what it does not do is give the client anything to recognise the URI by.

## Known defects

1. **Two incompatible actor properties.** `collections` (builder, array of URIs) vs
   `firm:collections` (server, index URI). Neither is read by the server; only the client
   reads one, and only in a form the data does not use.
2. **Client discovery cannot match the data.** As above: URI string vs object-typed
   predicate. Consequence: no bookmarks UI in practice.
3. **`firm:Bookmarks` has no code owner.** No constant in `FIRM_NS` and no test; the
   namespace is declared only by the context file described above, which nothing serves or
   references yet. The stored collection carries no `@context` at all, and serving adds
   only the AS2 context (`serialization.py:85`), so a reader never learns the namespace
   from the document. It passes JSON Schema validation only because the schema accepts any
   prefixed type (`firm/server/schemas/activities-schema.json:79`).
4. **`Link` items are incompatible with the collection store.** The file store models
   collection membership as a log of URI strings. The migration that any store touch runs
   first (`firm/core/store/file_collections.py:168`) does
   `[str(item) for item in items]`, so a `Link` dict becomes its Python `repr`. *(verified:
   on a copy of the tenant, `count_items` — which a plain `GET` calls — rewrote the
   collection document to `{id, type, attributedTo, totalItems}`, dropped `items`, and
   `page_items` then returned four dict-repr strings.)* The served result is at best an
   empty collection with `totalItems: 4`; I did not exercise the full GET on a live server,
   so whether the owner sees an empty list or an error depends on how the remote fetcher
   rejects a non-URI. Either way the bookmarks are gone after first read.
5. **Bookmarks outlive their targets.** 6 of 31 items point at `Tombstone`s because
   `setup_bookmarks` runs after `setup_deletes`. Nothing filters or reports them.
6. **Annotations have no home in the write path.** The corpus has `comment`, stored as
   `Link.summary`. Generic `Add` writes only a URI membership, so a bookmark created
   through C2S can never carry an annotation.
7. **`Add` snapshots remote objects.** Bookmarking a peer's note copies it into the local
   public store (`outbox.py:403`) instead of referencing it, so the bookmark list and the
   local copy can diverge from the peer's current state.
8. **No inbound path.** Peers cannot add to or remove from a collection here, so bookmark
   collections cannot federate even as a private-to-self construct.
9. **The live store has no bookmark data at all**, and the builder has no scenario that
   produces any: only the standalone `add_bookmarks.py` script does.

## Design questions this leaves open

Recorded, not decided:

- **Vocabulary.** Is `firm:Bookmarks` the right term, or should the set be typed by the AS2
  collection alone and identified by a well-known property (`bookmarks`, mirroring `liked`)?
  Whatever is chosen needs a `FIRM_NS` constant and a declaration reachable from the actor
  document.
- **Discovery.** Inline typed collections in `actor.collections`, a map of name → URI, or
  the existing `firm:collections` index (one more fetch, but reuses the mechanism the
  server already writes).
- **Item representation.** `Link` + annotation (`summary`) is expressive but breaks the URI
  log store. The alternatives are URI membership plus a separate annotation document, or a
  store that can hold objects as members.
- **Privacy.** Owner-only currently falls out of `attributedTo` and the absence of
  recipients, not from a stated rule. If bookmarks are meant to be private, that deserves a
  test; if they are meant to be public, they need recipients.
- **Federation.** Whether bookmarks ever leave the server (an `Add` to a peer's collection,
  a federated private collection via recipients) or stay a local construct.
- **Annotation writes.** Whether C2S needs a bookmark-specific activity or an extended
  `Add` that carries the note attached to the membership.

## Reproducing the numbers

```bash
cd ~/dev/fediverse

# 11 collections, 31 items, 11 actor docs with a "collections" key
grep -rl '"firm:Bookmarks"' firm-data.bak/tenants/*/public | wc -l
grep -rl '"firm:Bookmarks"' firm-data/tenants/*/public | wc -l      # 0

# item health: live notes vs tombstones
python3 - <<'PY'
import json, glob
store = 'firm-data.bak/tenants/firm.stevebate.dev/public'
docs = {d['id']: d for d in (json.load(open(p)) for p in glob.glob(store + '/*.json'))}
for d in docs.values():
    if isinstance(d.get('type'), list) and 'firm:Bookmarks' in d['type']:
        for item in d['items']:
            print(docs[item['href']]['type'], item['href'])
PY

# corpus side
cd firm-tools && python3 -c "
import json, glob
for p in glob.glob('social-data/*/actor.json'):
    d = json.load(open(p))
    print(p, len(d.get('bookmarks', [])))"
```
