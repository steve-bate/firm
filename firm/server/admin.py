from http import HTTPStatus
from typing import Mapping

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from firm.core.interfaces import Tenant

router = APIRouter(prefix="/admin")


def _not_implemented(request: Request) -> JSONResponse:
    return JSONResponse({"error": "Not implemented"}, status_code=HTTPStatus.NOT_IMPLEMENTED)


# =============================================================================
# 2. Server Configuration & Global Management
# =============================================================================

# 2.1 Server configuration


@router.get("/server/config")
async def get_server_config(request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.patch("/server/config")
async def patch_server_config(request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.get("/server/features")
async def get_server_features(request: Request) -> JSONResponse:
    return _not_implemented(request)


# 2.2 Server statistics


@router.get("/server/stats")
async def get_server_stats(request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.get("/server/health")
async def get_server_health(request: Request) -> JSONResponse:
    return Response("OK", status_code=HTTPStatus.OK)


@router.get("/server/metrics")
async def get_server_metrics(request: Request) -> JSONResponse:
    return _not_implemented(request)


# 2.3 System-level actions


@router.post("/server/actions/reload-config")
async def reload_config(request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/server/actions/rebuild-indexes")
async def rebuild_indexes(request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/server/actions/rotate-keys")
async def rotate_keys(request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/server/actions/flush-caches")
async def flush_caches(request: Request) -> JSONResponse:
    return _not_implemented(request)


# =============================================================================
# 3. Tenants
# =============================================================================

# 3.1 Tenant collection


@router.get("/tenants")
async def list_tenants(request: Request) -> JSONResponse:
    tenants: Mapping[str, Tenant] = request.app.state.tenants
    tenant_docs = [await t.public_store.get(t.prefix) for t in tenants.values()]
    return JSONResponse({"tenants": tenant_docs})


@router.get("/tenants/search")
async def search_tenants(request: Request) -> JSONResponse:
    return _not_implemented(request)


class CreateTenantRequest(BaseModel):
    prefix: str
    name: str
    summary: str


@router.post("/tenants")
async def create_tenant(body: CreateTenantRequest, request: Request) -> JSONResponse:
    # print(body)
    return _not_implemented(request)


# 3.2 Individual tenant


@router.get("/tenants/{tenant_id}")
async def get_tenant(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.patch("/tenants/{tenant_id}")
async def patch_tenant(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/tenants/{tenant_id}/actions/enable")
async def enable_tenant(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/tenants/{tenant_id}/actions/disable")
async def disable_tenant(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# 3.3 Tenant statistics


@router.get("/tenants/{tenant_id}/stats")
async def get_tenant_stats(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# =============================================================================
# 4. Actors (within Tenant)
# =============================================================================

# 4.1 Actor collection


@router.get("/tenants/{tenant_id}/actors")
async def list_actors(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.get("/tenants/{tenant_id}/actors/search")
async def search_actors(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/tenants/{tenant_id}/actors")
async def create_actor(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# 4.2 Individual actor


@router.get("/tenants/{tenant_id}/actors/{actor_id}")
async def get_actor(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.patch("/tenants/{tenant_id}/actors/{actor_id}")
async def patch_actor(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.delete("/tenants/{tenant_id}/actors/{actor_id}")
async def delete_actor(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/tenants/{tenant_id}/actors/{actor_id}/actions/enable")
async def enable_actor(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/tenants/{tenant_id}/actors/{actor_id}/actions/disable")
async def disable_actor(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# 4.3 Actor statistics


@router.get("/tenants/{tenant_id}/actors/{actor_id}/stats")
async def get_actor_stats(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# =============================================================================
# 5. Actor Outbox Management
# =============================================================================

# 5.1 List outbox items


@router.get("/tenants/{tenant_id}/actors/{actor_id}/outbox")
async def list_outbox(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# 5.2 Manipulate outbox items


@router.get("/tenants/{tenant_id}/actors/{actor_id}/outbox/{activity_id}")
async def get_outbox_item(
    tenant_id: str, actor_id: str, activity_id: str, request: Request
) -> JSONResponse:
    return _not_implemented(request)


@router.patch("/tenants/{tenant_id}/actors/{actor_id}/outbox/{activity_id}")
async def patch_outbox_item(
    tenant_id: str, actor_id: str, activity_id: str, request: Request
) -> JSONResponse:
    return _not_implemented(request)


@router.delete("/tenants/{tenant_id}/actors/{actor_id}/outbox/{activity_id}")
async def delete_outbox_item(
    tenant_id: str, actor_id: str, activity_id: str, request: Request
) -> JSONResponse:
    return _not_implemented(request)


@router.post("/tenants/{tenant_id}/actors/{actor_id}/outbox/{activity_id}/actions/redeliver")
async def redeliver_outbox_item(
    tenant_id: str, actor_id: str, activity_id: str, request: Request
) -> JSONResponse:
    return _not_implemented(request)


# =============================================================================
# 6. Actor Inbox Management
# =============================================================================

# 6.1 List inbox items


@router.get("/tenants/{tenant_id}/actors/{actor_id}/inbox")
async def list_inbox(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# 6.2 Manipulate inbox


@router.delete("/tenants/{tenant_id}/actors/{actor_id}/inbox/{activity_id}")
async def delete_inbox_item(
    tenant_id: str, actor_id: str, activity_id: str, request: Request
) -> JSONResponse:
    return _not_implemented(request)


@router.post("/tenants/{tenant_id}/actors/{actor_id}/inbox/actions/clear")
async def clear_inbox(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/tenants/{tenant_id}/actors/{actor_id}/inbox/{activity_id}/actions/reprocess")
async def reprocess_inbox_item(
    tenant_id: str, actor_id: str, activity_id: str, request: Request
) -> JSONResponse:
    return _not_implemented(request)


# =============================================================================
# 7. Actor Relationships (Follows, Blocks, Mutes)
# =============================================================================

# 7.1 Relationship listing


@router.get("/tenants/{tenant_id}/actors/{actor_id}/relationships/following")
async def list_following(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.get("/tenants/{tenant_id}/actors/{actor_id}/relationships/followers")
async def list_followers(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.get("/tenants/{tenant_id}/actors/{actor_id}/relationships/blocks")
async def list_blocks(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.get("/tenants/{tenant_id}/actors/{actor_id}/relationships/mutes")
async def list_mutes(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# 7.2 Relationship management


@router.post("/tenants/{tenant_id}/actors/{actor_id}/relationships/follow")
async def follow_actor(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/tenants/{tenant_id}/actors/{actor_id}/relationships/block")
async def block_actor(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/tenants/{tenant_id}/actors/{actor_id}/relationships/mute")
async def mute_actor(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.delete("/tenants/{tenant_id}/actors/{actor_id}/relationships/follow/{relationship_id}")
async def delete_follow(
    tenant_id: str, actor_id: str, relationship_id: str, request: Request
) -> JSONResponse:
    return _not_implemented(request)


@router.delete("/tenants/{tenant_id}/actors/{actor_id}/relationships/block/{relationship_id}")
async def delete_block(
    tenant_id: str, actor_id: str, relationship_id: str, request: Request
) -> JSONResponse:
    return _not_implemented(request)


@router.delete("/tenants/{tenant_id}/actors/{actor_id}/relationships/mute/{relationship_id}")
async def delete_mute(
    tenant_id: str, actor_id: str, relationship_id: str, request: Request
) -> JSONResponse:
    return _not_implemented(request)


# =============================================================================
# 8. Remote Delivery (Tenant-scoped)
# =============================================================================

# 8.1 Delivery configuration


@router.get("/tenants/{tenant_id}/delivery/config")
async def get_delivery_config(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.patch("/tenants/{tenant_id}/delivery/config")
async def patch_delivery_config(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# 8.2 Delivery statistics


@router.get("/tenants/{tenant_id}/delivery/stats")
async def get_delivery_stats(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# 8.3 Delivery queue / items


@router.get("/tenants/{tenant_id}/delivery")
async def list_deliveries(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.get("/tenants/{tenant_id}/delivery/{delivery_id}")
async def get_delivery(tenant_id: str, delivery_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.delete("/tenants/{tenant_id}/delivery/{delivery_id}")
async def delete_delivery(tenant_id: str, delivery_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/tenants/{tenant_id}/delivery/{delivery_id}/actions/retry")
async def retry_delivery(tenant_id: str, delivery_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/tenants/{tenant_id}/delivery/actions/retry-failed")
async def retry_failed_deliveries(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# =============================================================================
# 9. Files and Media (Tenant-scoped)
# =============================================================================

# 9.1 Files


@router.get("/tenants/{tenant_id}/files")
async def list_files(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.get("/tenants/{tenant_id}/files/{file_id}")
async def get_file(tenant_id: str, file_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.delete("/tenants/{tenant_id}/files/{file_id}")
async def delete_file(tenant_id: str, file_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/tenants/{tenant_id}/files/gc")
async def gc_files(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# 9.2 Media subset


@router.get("/tenants/{tenant_id}/media")
async def list_media(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.delete("/tenants/{tenant_id}/media/{media_id}")
async def delete_media(tenant_id: str, media_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# =============================================================================
# 10. OAuth Clients, Authorizations, Tokens (Tenant-scoped)
# =============================================================================

# 10.1 OAuth clients


@router.get("/tenants/{tenant_id}/oauth/clients")
async def list_oauth_clients(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/tenants/{tenant_id}/oauth/clients")
async def create_oauth_client(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.get("/tenants/{tenant_id}/oauth/clients/{client_id}")
async def get_oauth_client(tenant_id: str, client_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.patch("/tenants/{tenant_id}/oauth/clients/{client_id}")
async def patch_oauth_client(tenant_id: str, client_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.delete("/tenants/{tenant_id}/oauth/clients/{client_id}")
async def delete_oauth_client(tenant_id: str, client_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# 10.2 Authorizations


@router.get("/tenants/{tenant_id}/oauth/authorizations")
async def list_oauth_authorizations(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.delete("/tenants/{tenant_id}/oauth/authorizations/{authorization_id}")
async def delete_oauth_authorization(
    tenant_id: str, authorization_id: str, request: Request
) -> JSONResponse:
    return _not_implemented(request)


# 10.3 Tokens


@router.get("/tenants/{tenant_id}/oauth/tokens")
async def list_oauth_tokens(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.delete("/tenants/{tenant_id}/oauth/tokens/{token_id}")
async def delete_oauth_token(tenant_id: str, token_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/tenants/{tenant_id}/oauth/tokens/actions/revoke-by-client")
async def revoke_oauth_tokens_by_client(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# =============================================================================
# 11. Remote Cache (Global or Tenant-scoped)
# =============================================================================

# 11.1 Cache entries


@router.get("/remote-cache")
async def list_remote_cache(request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.get("/remote-cache/{cache_id}")
async def get_remote_cache_entry(cache_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# 11.2 Cache control


@router.delete("/remote-cache/{cache_id}")
async def delete_remote_cache_entry(cache_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/remote-cache/actions/clear")
async def clear_remote_cache(request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/remote-cache/{cache_id}/actions/refresh")
async def refresh_remote_cache_entry(cache_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# =============================================================================
# 12. Audit, Logs, and Security
# =============================================================================

# 12.1 Admin audit log


@router.get("/audit/admin-actions")
async def list_admin_audit_log(request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.get("/audit/admin-actions/{audit_id}")
async def get_admin_audit_entry(audit_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# 12.2 Security / abuse controls


@router.get("/security/domains")
async def list_domain_rules(request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/security/domains")
async def create_domain_rule(request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.delete("/security/domains/{rule_id}")
async def delete_domain_rule(rule_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


def create_admin_router() -> APIRouter:
    return router
