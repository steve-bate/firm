import logging
import mimetypes
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.templating import Jinja2Templates

from firm.core.interfaces import (
    FIRM_NS,
    HttpRequest,
    HttpResponse,
    ResourceStore,
    Tenant,
)
from firm.core.util import get_version

log = logging.getLogger(__name__)

STATIC_DIR = "firm.server/html/static"


# TODO Determine if html_static_endpoint is still needed
def html_static_endpoint(request: Request):
    tenant = request.state.tenant
    request_file_path = request.path_params["file_path"]
    for static_dir in [STATIC_DIR, tenant.files]:
        file_path = os.path.join(static_dir, request_file_path)
        if os.path.exists(file_path):
            mime_type, _ = mimetypes.guess_type(file_path)
            mime_type = mime_type or "application/octet-stream"
            return FileResponse(file_path, media_type=mime_type)
    return Response("File not found", status_code=404)


async def _get_timeline(tenant: Tenant, actor: dict) -> list[dict[str, Any]]:
    activity_uris = (await tenant.public_store.get(actor["outbox"])).get("orderedItems", [])
    activities = reversed([await tenant.public_store.get(item) for item in activity_uris[-10:]])
    content = {}
    for activity in activities:
        if activity.get("type") in ["Create", "Update", "Delete"]:
            if object_uri := activity.get("object"):
                if not object_uri.startswith(tenant.prefix):
                    continue
                if object_ := await tenant.public_store.get(object_uri):
                    content[activity["id"]] = object_
    return list(content.values())


async def _actor_context(uri: str, tenant: Tenant) -> dict[str, Any]:
    context = {}
    credentials = await tenant.private_store.query_one(
        {
            "type": FIRM_NS.Credentials.value,
            "attributedTo": uri,
        }
    )
    context["roles"] = credentials.get(FIRM_NS.role.value, []) if credentials else []
    actor = await tenant.public_store.get(uri)
    context["timeline"] = await _get_timeline(tenant, actor)
    return context


async def _doc_context(uri: str, store: ResourceStore):
    context: dict[str, Any] = {}
    # Add any specific document context logic here
    return context


@dataclass
class TemplateConfig:
    template: str
    context: Callable[[str, ResourceStore], Awaitable[dict[str, Any]]] | None = None


ACTOR_TEMPLATE = TemplateConfig("actor.jinja2", _actor_context)
DOCUMENT_TEMPLATE = TemplateConfig("document.jinja2", _doc_context)

RESOURCE_TEMPLATES: dict[str, TemplateConfig | str] = {
    "Person": ACTOR_TEMPLATE,
    "Organization": ACTOR_TEMPLATE,
    "Group": ACTOR_TEMPLATE,
    "Application": ACTOR_TEMPLATE,
    "Service": ACTOR_TEMPLATE,
    "Note": DOCUMENT_TEMPLATE,
    "Article": DOCUMENT_TEMPLATE,
}


def _format_datetime(value, format="%b %d, %Y at %H:%M"):
    """Format a datetime string to a nice readable format."""
    if not value:
        return ""
    try:
        # Parse ISO 8601 datetime string
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            dt = value

        # Convert to local timezone
        local_tz = timezone.utc  # Default to UTC, consider user's timezone in production
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local_dt = dt.astimezone(local_tz)

        return local_dt.strftime(format)
    except Exception:
        return value


_tenant_templates: dict[str, Jinja2Templates] = {}


def _get_tenant_templates(tenant: Tenant) -> Jinja2Templates:
    if templates := _tenant_templates.get(tenant.prefix):
        return templates
    default_templates = Jinja2Templates(directory="firm.server/html/templates")
    templates_dir = os.path.join(
        tenant.files,
        "templates",
    )
    if os.path.exists(templates_dir):
        templates = Jinja2Templates(directory=[templates_dir, "firm.server/html/templates"])
    else:
        templates = default_templates

    templates.env.tests["match"] = lambda value, pattern: re.match(pattern, value)

    templates.env.filters["format_datetime"] = _format_datetime
    _tenant_templates[tenant.prefix] = templates
    return templates


async def html_endpoint(request: HttpRequest) -> HttpResponse:
    tenant = request.state.tenant
    templates = _get_tenant_templates(tenant)
    request_url = str(request.url)
    if request_url == tenant.prefix or request_url == tenant.prefix + "/":
        users = await tenant.public_store.query(
            {
                "type": "Person",
            }
        )
        return templates.TemplateResponse(
            "home.jinja2",
            dict(
                request=request,
                get_version=get_version,
                users=users,
            ),
        )
    elif request.url.path == "/login":
        return templates.TemplateResponse(
            "login.jinja2",
            dict(
                request=request,
                get_version=get_version,
            ),
        )
    else:
        # Check for file first
        file_path = tenant.files / request.url.path[1:]
        if os.path.exists(file_path):
            mime_type, _ = mimetypes.guess_type(file_path)
            mime_type = mime_type or "application/octet-stream"
            return FileResponse(file_path, media_type=mime_type)
        resource = await tenant.public_store.get(str(request.url))
        if not resource:
            return Response("Resource not found", status_code=404)
        else:
            if template_config := RESOURCE_TEMPLATES.get(resource.get("type")):
                if isinstance(template_config, str):
                    template_config = TemplateConfig(template_config)
                context = dict(
                    request=request,
                    get_version=get_version,
                    resource=resource,
                )
                if template_config.context:
                    context.update(await template_config.context(str(request.url), tenant))
                return templates.TemplateResponse(template_config.template, context)
            return JSONResponse(resource)
