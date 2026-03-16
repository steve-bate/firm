"""
FastAPI router for the ActivityPub MediaUpload protocol.
https://www.w3.org/wiki/SocialCG/ActivityPub/MediaUpload

HTTP-independent business logic lives in ``firm.core.services.media_upload``.
"""

import json
import logging
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.responses import FileResponse, Response

from firm.core.interfaces import Principal
from firm.core.media.service import (
    InvalidMediaUploadException,
    MediaUploadService,
    NotAuthorizedException,
)
from firm.server.auth import get_principal

log = logging.getLogger(__name__)

_UPLOAD_PATH = "/media/upload"
_SERVE_PATH = "/actors/{username}/media/{filename}"


def create_media_upload_router() -> APIRouter:
    """Return an ``APIRouter`` that provides:

    * ``POST /media/upload``  — accept a ``multipart/form-data`` upload.
    * ``GET  /actors/{username}/media/{filename}`` — serve stored files.

    The router's lifespan registers the ``uploadMedia`` URL in every
    tenant's ``endpoints`` mapping so that client-to-server ActivityPub
    clients can discover it from the actor profile.
    """
    service = MediaUploadService()

    @asynccontextmanager
    async def _lifespan(app):
        for tenant in app.state.tenants.values():
            tenant.endpoints["uploadMedia"] = f"{tenant.prefix}{_UPLOAD_PATH}"
        yield

    router = APIRouter(lifespan=_lifespan)

    @router.post(_UPLOAD_PATH)
    async def upload_media(
        request: Request,
        principal: Principal | None = Depends(get_principal),
    ) -> Response:
        """Accept a ``multipart/form-data`` upload as described in the
        ActivityPub MediaUpload protocol.

        Expected form fields
        --------------------
        file
            The binary media file.
        object
            A JSON-serialised ActivityStreams object shell (may also be a
            ``Create`` wrapper).  The server assigns ``id`` and ``url``.

        Returns ``201 Created`` with a ``Location`` header pointing to the
        newly created object's URI.
        """
        if not principal:
            raise HTTPException(HTTPStatus.UNAUTHORIZED, "Authentication required")

        form = await request.form()
        file_field = form.get("file")
        object_field = form.get("object")

        if file_field is None or object_field is None:
            raise HTTPException(
                HTTPStatus.BAD_REQUEST, "Both 'file' and 'object' form fields are required"
            )

        # file_field is an UploadFile from FastAPI/Starlette
        file_data: bytes = await file_field.read()  # type: ignore[union-attr]
        content_type: str = getattr(file_field, "content_type", None) or "application/octet-stream"
        filename: str = getattr(file_field, "filename", None) or "upload"

        # object_field may be a plain string or an UploadFile (when the part
        # carries its own Content-Type header, Starlette wraps it in UploadFile).
        if isinstance(object_field, str):
            raw_object = object_field
        else:
            raw_object = (await object_field.read()).decode()  # type: ignore[union-attr]

        try:
            object_shell = json.loads(raw_object)
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(HTTPStatus.BAD_REQUEST, "The 'object' field must be valid JSON")

        try:
            media_object = await service.process_upload(
                tenant=request.state.tenant,
                principal=principal,
                object_shell=object_shell,
                file_data=file_data,
                content_type=content_type,
                filename=filename,
            )
        except NotAuthorizedException as e:
            raise HTTPException(HTTPStatus.FORBIDDEN, detail=str(e))
        except InvalidMediaUploadException as e:
            raise HTTPException(HTTPStatus.BAD_REQUEST, detail=str(e))

        print(json.dumps(media_object, indent=2))
        return JSONResponse(
            status_code=HTTPStatus.CREATED.value,
            headers={"Location": media_object["id"]},
            content=media_object,
        )

    @router.get(_SERVE_PATH)
    async def serve_media(request: Request, username: str, filename: str) -> Response:
        """Serve a previously uploaded media file.

        Path traversal is prevented by accepting only the bare filename
        (no directory separators or parent-directory segments).
        """
        # Reject any attempt to escape the actor's files directory.
        safe_filename = filename.replace("\\", "/")
        if ".." in safe_filename or "/" in safe_filename:
            raise HTTPException(HTTPStatus.BAD_REQUEST, "Invalid filename")
        safe_username = username.replace("\\", "/")
        if ".." in safe_username or "/" in safe_username:
            raise HTTPException(HTTPStatus.BAD_REQUEST, "Invalid username")

        tenant = request.state.tenant
        file_path = (tenant.files / safe_username / "media" / safe_filename).resolve()

        # Ensure the resolved path is still inside the tenant files directory.
        try:
            file_path.relative_to(tenant.files.resolve())
        except ValueError:
            raise HTTPException(HTTPStatus.BAD_REQUEST, "Invalid path")

        if not file_path.is_file():
            raise HTTPException(HTTPStatus.NOT_FOUND, "File not found")

        return FileResponse(file_path)

    return router
