"""
FastAPI router for the ActivityPub MediaUpload protocol.
https://www.w3.org/wiki/SocialCG/ActivityPub/MediaUpload

This module is the HTTP adapter and nothing else: it takes the request's form
parts apart and maps the service's outcomes to status codes. What an upload
must contain, and which paths are servable, is decided in
``firm.core.media.service``.
"""

from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.responses import FileResponse, Response

from firm.core.interfaces import Principal
from firm.core.media.service import (
    InvalidMediaPathException,
    InvalidMediaUploadException,
    MediaNotFoundException,
    MediaUploadService,
    NotAuthorizedException,
)
from firm.server.auth import get_principal

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

        # The object part arrives as text, or as an UploadFile when the client
        # gives the part its own Content-Type header.
        object_text: str | None
        if isinstance(object_field, str):
            object_text = object_field
        elif object_field is None:
            object_text = None
        else:
            object_text = (await object_field.read()).decode()

        file_data: bytes | None
        if file_field is None:
            file_data = None
        elif isinstance(file_field, str):
            file_data = file_field.encode()
        else:
            file_data = await file_field.read()

        try:
            media_object = await service.process_upload(
                tenant=request.state.tenant,
                principal=principal,
                object_text=object_text,
                file_data=file_data,
                content_type=getattr(file_field, "content_type", None)
                or "application/octet-stream",
                filename=getattr(file_field, "filename", None) or "upload",
            )
        except NotAuthorizedException as e:
            raise HTTPException(HTTPStatus.FORBIDDEN, detail=str(e))
        except InvalidMediaUploadException as e:
            raise HTTPException(HTTPStatus.BAD_REQUEST, detail=str(e))

        return JSONResponse(
            status_code=HTTPStatus.CREATED.value,
            headers={"Location": str(media_object["id"])},
            content=media_object,
        )

    @router.get(_SERVE_PATH)
    async def serve_media(request: Request, username: str, filename: str) -> Response:
        """Serve a previously uploaded media file.

        The service accepts only a bare username and filename, and refuses any
        path that resolves outside the tenant's files directory — which is what
        a symlink pointing elsewhere would otherwise get past.
        """
        try:
            file_path = service.resolve_media_path(request.state.tenant, username, filename)
        except InvalidMediaPathException as e:
            raise HTTPException(HTTPStatus.BAD_REQUEST, detail=str(e))
        except MediaNotFoundException as e:
            raise HTTPException(HTTPStatus.NOT_FOUND, detail=str(e))

        return FileResponse(file_path)

    return router
