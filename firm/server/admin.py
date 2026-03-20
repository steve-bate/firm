import logging
import os
import secrets
from http import HTTPStatus
from typing import Mapping
from urllib.parse import urlparse

import yaml
from attr import dataclass
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from firm.core.interfaces import Tenant
from firm.core.util import is_actor_object

log = logging.getLogger(__name__)

_http_basic = HTTPBasic(realm="FIRM Administration")


@dataclass
class AdminIdentity:
    username: str


async def _authenticate(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(_http_basic),
) -> None:
    config = request.app.state.config
    if config.admin:
        for user in config.admin.users:
            username_match = secrets.compare_digest(
                credentials.username.encode("utf-8"), user.username.encode("utf-8")
            )
            password_match = secrets.compare_digest(
                credentials.password.encode("utf-8"), user.password.encode("utf-8")
            )
            if username_match and password_match:
                identity = AdminIdentity(username=credentials.username)
                request.state.user = identity
                return
    raise HTTPException(
        status_code=HTTPStatus.UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": 'Basic realm="FIRM Administration"'},
    )


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(_authenticate)])


def _not_implemented(request: Request) -> JSONResponse:
    return JSONResponse({"error": "Not implemented"}, status_code=HTTPStatus.NOT_IMPLEMENTED)


# =============================================================================
# Server Configuration & Global Management
# =============================================================================

# Server configuration


@router.get("/")
async def admin_root(request: Request) -> JSONResponse:
    identity = request.state.user
    return JSONResponse({"message": f"{identity.username}: Welcome to the FIRM admin API"})


@router.get("/server/config")
async def get_server_config() -> JSONResponse:
    config_file = os.environ.get("FIRM_CONFIG")
    if not config_file:
        return JSONResponse(
            {"error": "Server configuration not found"},
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        )
    with open(config_file, "r") as f:
        config_data = yaml.load(f.read(), Loader=yaml.SafeLoader)
        del config_data["admin"]  # Don't expose admin credentials
    return JSONResponse({"config": config_data})


# @router.patch("/server/config")
# async def patch_server_config(request: Request) -> JSONResponse:
#     return _not_implemented(request)


# Server statistics

# @router.get("/server/stats")
# async def get_server_stats(request: Request) -> JSONResponse:
#     return _not_implemented(request)

# @router.get("/server/metrics")
# async def get_server_metrics(request: Request) -> JSONResponse:
#     return _not_implemented(request)


# =============================================================================
# Tenants
# =============================================================================

# Tenant collection


@router.get("/tenants")
async def list_tenants(request: Request) -> JSONResponse:
    tenants: Mapping[str, Tenant] = request.app.state.tenants
    tenant_docs = [await t.public_store.get(t.prefix) for t in tenants.values()]
    return JSONResponse({"tenants": tenant_docs})


# @router.get("/tenants/search")
# async def search_tenants(request: Request) -> JSONResponse:
#     return _not_implemented(request)


class CreateTenantRequest(BaseModel):
    prefix: str
    name: str
    summary: str


@router.post("/tenants")
async def create_tenant(body: CreateTenantRequest, request: Request) -> JSONResponse:
    # print(body)
    return _not_implemented(request)


# Individual tenant


def _tenant(tenant_id: str, request: Request) -> Tenant:
    tenant = next(
        (t for t in request.app.state.tenants.values() if urlparse(t.prefix).netloc == tenant_id),
        None,
    )
    if not tenant:
        raise HTTPException(status_code=404, detail=f"Tenant not found: {tenant_id}")
    return tenant


async def _tenant_doc(tenant_id: str, request: Request):
    tenant = _tenant(tenant_id, request)
    tenant_doc = await tenant.public_store.get(tenant.prefix)
    if not tenant_doc:
        raise HTTPException(status_code=404, detail=f"Tenant document not found: {tenant_id}")
    return tenant_doc


@router.get("/tenants/{tenant_id}")
async def get_tenant(tenant_id: str, request: Request) -> JSONResponse:
    tenant_doc = await _tenant_doc(tenant_id, request)
    return JSONResponse(tenant_doc)


class TenantPatchRequest(BaseModel):
    name: str | None = None
    summary: str | None = None


@router.patch("/tenants/{tenant_id}")
async def patch_tenant(tenant_id: str, body: TenantPatchRequest, request: Request) -> JSONResponse:
    tenant_doc = await _tenant_doc(tenant_id, request)
    tenant_doc |= body.model_dump(exclude_unset=True)
    tenant = _tenant(tenant_id, request)
    await tenant.public_store.put(tenant_doc)
    return JSONResponse(tenant_doc)


# @router.delete("/tenants/{tenant_id}")
# async def delete_tenant(tenant_id: str, request: Request) -> JSONResponse:
#     return _not_implemented(request)


# @router.post("/tenants/{tenant_id}/actions/enable")
# async def enable_tenant(tenant_id: str, request: Request) -> JSONResponse:
#     return _not_implemented(request)


# @router.post("/tenants/{tenant_id}/actions/disable")
# async def disable_tenant(tenant_id: str, request: Request) -> JSONResponse:
#     return _not_implemented(request)


# Tenant statistics


class TenantStatsResponse(BaseModel):
    actor_count: int
    document_count: int


@router.get("/tenants/{tenant_id}/stats")
async def get_tenant_stats(tenant_id: str, request: Request) -> JSONResponse:
    tenant = _tenant(tenant_id, request)
    actor_count = 0
    document_count = 0
    for doc in await tenant.public_store.query({}):
        document_count += 1
        if is_actor_object(doc):
            actor_count += 1

    return JSONResponse(
        TenantStatsResponse(
            actor_count=actor_count,
            document_count=document_count,
        ).model_dump()
    )


# =============================================================================
# Actors (within Tenant)
# =============================================================================

# Actor collection


@router.get("/tenants/{tenant_id}/actors")
async def list_actors(tenant_id: str, request: Request) -> JSONResponse:
    tenant = _tenant(tenant_id, request)
    actor_docs = []
    for doc in await tenant.public_store.query({}):
        if is_actor_object(doc):
            actor_docs.append(doc)
    return JSONResponse({"actors": actor_docs})


@router.get("/tenants/{tenant_id}/actors/search")
async def search_actors(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/tenants/{tenant_id}/actors")
async def create_actor(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# Individual actor


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


# Actor statistics


@router.get("/tenants/{tenant_id}/actors/{actor_id}/stats")
async def get_actor_stats(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# =============================================================================
# Actor Outbox Management
# =============================================================================

# List outbox items


@router.get("/tenants/{tenant_id}/actors/{actor_id}/outbox")
async def list_outbox(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# Manipulate outbox items


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
# Actor Inbox Management
# =============================================================================

# List inbox items


@router.get("/tenants/{tenant_id}/actors/{actor_id}/inbox")
async def list_inbox(tenant_id: str, actor_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# Manipulate inbox


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
# Actor Relationships (Follows, Blocks, Mutes)
# =============================================================================

# Relationship listing


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


# Relationship management


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
# Remote Delivery (Tenant-scoped)
# =============================================================================

# Delivery configuration


@router.get("/tenants/{tenant_id}/delivery/config")
async def get_delivery_config(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.patch("/tenants/{tenant_id}/delivery/config")
async def patch_delivery_config(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# Delivery statistics


@router.get("/tenants/{tenant_id}/delivery/stats")
async def get_delivery_stats(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# Delivery queue / items


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
# Files and Media (Tenant-scoped)
# =============================================================================

# Files


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


# Media subset


@router.get("/tenants/{tenant_id}/media")
async def list_media(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.delete("/tenants/{tenant_id}/media/{media_id}")
async def delete_media(tenant_id: str, media_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# =============================================================================
# OAuth Clients, Authorizations, Tokens (Tenant-scoped)
# =============================================================================

# OAuth clients


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


# Authorizations


@router.get("/tenants/{tenant_id}/oauth/authorizations")
async def list_oauth_authorizations(tenant_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.delete("/tenants/{tenant_id}/oauth/authorizations/{authorization_id}")
async def delete_oauth_authorization(
    tenant_id: str, authorization_id: str, request: Request
) -> JSONResponse:
    return _not_implemented(request)


# Tokens


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
# Remote Cache (Global or Tenant-scoped)
# =============================================================================

# Cache entries


@router.get("/remote-cache")
async def list_remote_cache(request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.get("/remote-cache/{cache_id}")
async def get_remote_cache_entry(cache_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# Cache control


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
# Audit, Logs, and Security
# =============================================================================

# Admin audit log


@router.get("/audit/admin-actions")
async def list_admin_audit_log(request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.get("/audit/admin-actions/{audit_id}")
async def get_admin_audit_entry(audit_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


# Security / abuse controls


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
