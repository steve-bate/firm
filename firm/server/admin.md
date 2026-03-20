## 1. API Surface and Conventions

- Base path: `/admin/api/v1`
- Authentication: admin-only mechanism (e.g., HTTP Basic or bearer token distinct from tenant OAuth).[^1]
- Response format: JSON, with:
    - `data`, `error`, `meta` envelopes
    - Standard pagination: `?page=1&per_page=50`
    - Standard search: `?q=text`, plus field filters (e.g., `?domain=example.com`).
- All tenant-scoped resources must include `tenant_id` in path (e.g., `/tenants/{tenant_id}/actors`).

***

## 2. Server Configuration \& Global Management

### 2.1 Server configuration

- GET `/server/config`
    - Current configuration snapshot (domains, ports, feature flags, storage backends, queue configuration, auth plugins, etc.).[^2][^1]
- PATCH `/server/config`
    - Update selected configuration keys (with validation and possibly restart hints).
- GET `/server/features`
    - Lists enabled/disabled feature flags (e.g., authz plugins, ActivityPub extensions).


### 2.2 Server statistics

- GET `/server/stats`
    - Aggregate statistics:
        - Total tenants, actors, objects, activities
        - Deliveries per time window (last hour/day)
        - Error counts (5xx, 4xx), queue depth, worker status.
- GET `/server/health`
    - High-level health: database connectivity, cache status, queue status.
- GET `/server/metrics`
    - Detailed metrics suitable for scraping (if you want to wrap Prometheus later).


### 2.3 System-level actions

- POST `/server/actions/reload-config`
- POST `/server/actions/rebuild-indexes`
- POST `/server/actions/rotate-keys`
    - For rotating server-side signing keys used in ActivityPub HTTP signatures.[^1]
- POST `/server/actions/flush-caches`
    - Global caches (distinct from tenant remote cache).

***

## 3. Tenants

### 3.1 Tenant collection

- GET `/tenants`
    - List all tenants with pagination and basic filters (e.g., `domain`, `enabled`, `search`).
- GET `/tenants/search`
    - Advanced search with filters on domain, human-friendly name, status, created_at range.
- POST `/tenants`
    - Create tenant:
        - Core fields: `id` (or slug), domain(s), display name, contact email
        - Configuration overlay: per-tenant AP settings, storage limits, auth policy, rate limits.[^1]


### 3.2 Individual tenant

- GET `/tenants/{tenant_id}`
- PATCH `/tenants/{tenant_id}`
    - Update tenant metadata and per-tenant configuration.
- DELETE `/tenants/{tenant_id}`
    - Optional soft-delete (status `deleted`) vs hard-delete flag.
- POST `/tenants/{tenant_id}/actions/enable`
- POST `/tenants/{tenant_id}/actions/disable`
    - Temporarily disable all actor activity and remote delivery for that tenant.


### 3.3 Tenant statistics

- GET `/tenants/{tenant_id}/stats`
    - Actor count, object count, activities per day, storage usage (local media + DB records), inbound/outbound federation volume.

***

## 4. Actors (within Tenant)

### 4.1 Actor collection

- GET `/tenants/{tenant_id}/actors`
    - List actors with filters: `type` (Person/Application/Service), `username`, `status` (active/disabled), `kind` of usage.[^1]
- GET `/tenants/{tenant_id}/actors/search`
    - Search on username, display name, preferred handle.
- POST `/tenants/{tenant_id}/actors`
    - Create actor:
        - Username, display name, summary
        - Actor type
        - Initial auth credentials or external auth binding depending on your design.


### 4.2 Individual actor

- GET `/tenants/{tenant_id}/actors/{actor_id}`
- PATCH `/tenants/{tenant_id}/actors/{actor_id}`
- DELETE `/tenants/{tenant_id}/actors/{actor_id}`
    - Optional: soft-delete vs hard-delete.
- POST `/tenants/{tenant_id}/actors/{actor_id}/actions/disable`
- POST `/tenants/{tenant_id}/actors/{actor_id}/actions/enable`


### 4.3 Actor statistics

- GET `/tenants/{tenant_id}/actors/{actor_id}/stats`
    - Follower/following counts, posts count, likes, boosts
    - Inbound/outbound activities per time window
    - Per-actor delivery errors (if tracked separately from tenant/global).

***

## 5. Actor Outbox Management

### 5.1 List and search outbox items

- GET `/tenants/{tenant_id}/actors/{actor_id}/outbox`
    - Paginated list of activities/objects with filters:
        - `type` (Create/Follow/Announce/etc.)
        - `object_type` (Note, Article, custom types)
        - `since`, `until` timestamps
        - `q` full-text search on content, id, or target.


### 5.2 Manipulate outbox items

- GET `/tenants/{tenant_id}/actors/{actor_id}/outbox/{activity_id}`
- PATCH `/tenants/{tenant_id}/actors/{actor_id}/outbox/{activity_id}`
    - Edit content or metadata where safe; may record admin edit history.
- DELETE `/tenants/{tenant_id}/actors/{actor_id}/outbox/{activity_id}`
    - Optionally trigger federated Delete/Undo or local-only removal, controlled via body flag.
- POST `/tenants/{tenant_id}/actors/{actor_id}/outbox/{activity_id}/actions/redeliver`
    - Force re-delivery to failed recipients.

***

## 6. Actor Inbox Management

### 6.1 Search inbox

- GET `/tenants/{tenant_id}/actors/{actor_id}/inbox`
    - List inbound activities with filters:
        - `type`, `object_type`, `from_domain`, `from_actor`
        - `status` (processed, failed, quarantined).


### 6.2 Manipulate inbox

- DELETE `/tenants/{tenant_id}/actors/{actor_id}/inbox/{activity_id}`
    - Remove a specific inbound activity (admin override).
- POST `/tenants/{tenant_id}/actors/{actor_id}/inbox/actions/clear`
    - Clear inbox, with options:
        - `before` timestamp
        - `status` filter
        - Soft vs hard delete.
- Optional:
    - POST `/tenants/{tenant_id}/actors/{actor_id}/inbox/{activity_id}/actions/reprocess`
        - Re-run ActivityPub processing pipeline for a specific activity, useful for debugging.[^2]

***

## 7. Actor Relationships (Follows, Blocks, Mutes)

### 7.1 Relationship listing

- GET `/tenants/{tenant_id}/actors/{actor_id}/relationships/following`
- GET `/tenants/{tenant_id}/actors/{actor_id}/relationships/followers`
- GET `/tenants/{tenant_id}/actors/{actor_id}/relationships/blocks`
- GET `/tenants/{tenant_id}/actors/{actor_id}/relationships/mutes` (if supported)

Each endpoint supports filters like `remote_only`, `domain`, `local_only`.

### 7.2 Relationship management

- POST `/tenants/{tenant_id}/actors/{actor_id}/relationships/follow`
    - Body: target actor URI/handle; optional flags about remote delivery.
- POST `/tenants/{tenant_id}/actors/{actor_id}/relationships/block`
- POST `/tenants/{tenant_id}/actors/{actor_id}/relationships/mute`
- DELETE `/tenants/{tenant_id}/actors/{actor_id}/relationships/follow/{relationship_id}`
- DELETE `/tenants/{tenant_id}/actors/{actor_id}/relationships/block/{relationship_id}`
- DELETE `/tenants/{tenant_id}/actors/{actor_id}/relationships/mute/{relationship_id}`

***

## 8. Remote Delivery (Tenant-scoped)

### 8.1 Delivery configuration

- GET `/tenants/{tenant_id}/delivery/config`
    - Per-tenant delivery settings: concurrency, timeouts, backoff strategy, per-domain limits.[^1]
- PATCH `/tenants/{tenant_id}/delivery/config`


### 8.2 Delivery statistics

- GET `/tenants/{tenant_id}/delivery/stats`
    - Aggregates:
        - Success/failure counts by time window
        - Average latency, P95/P99 latency
        - Retry counts, dead-letter queue size.


### 8.3 Delivery queue / items

Depending on your internal model, you can treat delivery attempts as queue items or logs.

- GET `/tenants/{tenant_id}/delivery`
    - List pending or recent deliveries with filters:
        - `status` (pending, succeeded, failed, dead-letter)
        - `domain` or `inbox_url`
        - `activity_id`
        - `since`, `until`.
- GET `/tenants/{tenant_id}/delivery/{delivery_id}`
- DELETE `/tenants/{tenant_id}/delivery/{delivery_id}`
    - Cancel or purge a queued or failed delivery.
- POST `/tenants/{tenant_id}/delivery/{delivery_id}/actions/retry`
- POST `/tenants/{tenant_id}/delivery/actions/retry-failed`
    - Bulk retry with filters (domain, time window).

***

## 9. Files and Media (Tenant-scoped)

### 9.1 Files

- GET `/tenants/{tenant_id}/files`
    - List stored files with filters:
        - `type` (media, attachment, avatar, header)
        - `actor_id`
        - `content_type`
        - `larger_than`, `smaller_than` sizes.
- GET `/tenants/{tenant_id}/files/{file_id}`
    - Metadata (paths/URLs, size, checksum, linked object IDs).
- DELETE `/tenants/{tenant_id}/files/{file_id}`
    - Delete file and optionally unlink associated objects.
- POST `/tenants/{tenant_id}/files/gc`
    - Run garbage-collection job for unreferenced files, with dry-run option.


### 9.2 Media subset

If you want a separate media-specific view:

- GET `/tenants/{tenant_id}/media`
    - Shortcut for `type=media`.
- DELETE `/tenants/{tenant_id}/media/{media_id}`

***

## 10. OAuth Clients, Authorizations, Tokens (Tenant-scoped)

Assuming you keep per-tenant OAuth2 configuration for client-to-server or user auth.[^3][^4]

### 10.1 OAuth clients

- GET `/tenants/{tenant_id}/oauth/clients`
- POST `/tenants/{tenant_id}/oauth/clients`
    - Fields: name, redirect_uris, scopes, confidential/public, client_secret strategy.
- GET `/tenants/{tenant_id}/oauth/clients/{client_id}`
- PATCH `/tenants/{tenant_id}/oauth/clients/{client_id}`
- DELETE `/tenants/{tenant_id}/oauth/clients/{client_id}`


### 10.2 Authorizations

- GET `/tenants/{tenant_id}/oauth/authorizations`
    - List grants: actor, client, scopes, issued_at, last_used_at.
- DELETE `/tenants/{tenant_id}/oauth/authorizations/{authorization_id}`
    - Revoke authorization (all tokens under this grant).


### 10.3 Tokens

- GET `/tenants/{tenant_id}/oauth/tokens`
    - Optional filters: `actor_id`, `client_id`, `active`, `scope`.
- DELETE `/tenants/{tenant_id}/oauth/tokens/{token_id}`
    - Revoke individual token.
- POST `/tenants/{tenant_id}/oauth/tokens/actions/revoke-by-client`
    - Bulk revoke tokens for a client (and optionally actor).

***

## 11. Remote Cache (Global or Tenant-scoped)

If remote cache entries are logically tenant-scoped, include `tenant_id`; otherwise keep global.

### 11.1 Cache entries

- GET `/remote-cache`
    - List entries, with filters:
        - `tenant_id` (optional)
        - `key`/prefix, `type` (actor, object, webfinger, HTTP resource)
        - `domain`, `status` (fresh, stale).
- GET `/remote-cache/{cache_id}`
    - Details: key, value summary, last_fetched, ttl, size.


### 11.2 Cache control

- DELETE `/remote-cache/{cache_id}`
    - Delete single entry.
- POST `/remote-cache/actions/clear`
    - Clear all entries; optional filters (tenant, type, domain).
- POST `/remote-cache/{cache_id}/actions/refresh`
    - Force re-fetch of remote resource for that key.

***

## 12. Audit, Logs, and Security (Optional but Strongly Recommended)

### 12.1 Admin audit log

- GET `/audit/admin-actions`
    - List admin operations with filters: `admin_id`, `resource_type`, `resource_id`, `action`, time range.
- GET `/audit/admin-actions/{audit_id}`


### 12.2 Security/abuse controls

- GET `/security/domains`
    - List per-tenant or global domain blocks/allow-lists.
- POST `/security/domains`
    - Add a block/allow rule.
- DELETE `/security/domains/{rule_id}`

***

## 13. Example Resource Hierarchy Table

A compact view you can keep in front of the coding agent:


| Area | Path pattern | Notes |
| :-- | :-- | :-- |
| Server config | `/server/config`, `/server/stats`, `/server/actions/*` | Global, admin-only |
| Tenants | `/tenants`, `/tenants/{tenant_id}` | Multi-tenant core |
| Tenant stats | `/tenants/{tenant_id}/stats` | Aggregate per tenant |
| Actors | `/tenants/{tenant_id}/actors`, `/tenants/{tenant_id}/actors/{actor}` | Actor lifecycle |
| Outbox | `/tenants/{tid}/actors/{aid}/outbox` | AP S2S outbound |
| Inbox | `/tenants/{tid}/actors/{aid}/inbox` | AP S2S inbound |
| Relationships | `/tenants/{tid}/actors/{aid}/relationships/*` | Follows, blocks, mutes |
| Delivery | `/tenants/{tid}/delivery*` | Remote delivery queue + stats |
| Files/media | `/tenants/{tid}/files*`, `/tenants/{tid}/media*` | Storage \& GC |
| OAuth | `/tenants/{tid}/oauth/*` | Clients, grants, tokens |
| Remote cache | `/remote-cache*` | Remote actor/object cache |
| Audit/security | `/audit/admin-actions`, `/security/domains` | Optional but useful |

Would you prefer to keep the admin API as a separate service (own auth, base URL) or mounted into the FIRM server app under a prefix like `/admin`?
<span style="display:none">[^10][^11][^12][^13][^14][^15][^5][^6][^7][^8][^9]</span>

<div align="center">⁂</div>

[^1]: https://www.stevebate.net/firm-released/

[^2]: https://www.stevebate.net/category/fediverse/firm/

[^3]: https://www.stevebate.net/firm-authorization-support/

[^4]: https://www.stevebate.net/activitypub-client-api-a-way-forward/

[^5]: https://github.com/steve-bate

[^6]: https://github.com/SteveBate

[^7]: https://www.github-zh.com/projects/850960708-firm-server

[^8]: https://www.stevebate.net/category/fediverse/activitypub/

[^9]: https://github.com/openhab/openhab/issues/1714

[^10]: https://motd.co/2025/02/fosdem-2025-postmarks/

[^11]: https://social.technoetic.com/@steve/114806186130243949

[^12]: https://social.technoetic.com/@steve

[^13]: https://socialhub.activitypub.rocks/t/account-migration/3058?page=4

[^14]: https://github.com/fmagrini/bayes-bay/issues/19

[^15]: https://vpl.bibliocommons.com/v2/record/S38C10821538
