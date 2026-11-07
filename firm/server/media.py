"""
FastAPI router for the ActivityPub MediaUpload protocol.
https://www.w3.org/wiki/SocialCG/ActivityPub/MediaUpload

This module is the HTTP adapter and nothing else: it takes the request's form
parts apart, streams the file to disk in chunks, and maps the service's outcomes
to status codes. What an upload must contain, and which paths are servable, is
decided in ``firm.core.media.service``.
"""

import hashlib
import os
import tempfile
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from typing import BinaryIO

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

#: Bytes copied per read. One chunk is the most a single upload holds in memory.
_COPY_CHUNK = 1024 * 1024


def _copy_stream_to_file(
    source: BinaryIO,
    destination: Path,
    *,
    chunk_size: int = _COPY_CHUNK,
    max_bytes: int | None = None,
) -> str:
    """Copy ``source`` to ``destination`` in chunks and return the SHA-256 digest.

    The caller renames the temporary file into place, so a truncated copy never
    becomes addressable and never leaves a half-written file behind.
    """
    digest = hashlib.sha256()
    written = 0
    with destination.open("wb") as target:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            written += len(chunk)
            if max_bytes is not None and written > max_bytes:
                target.close()
                destination.unlink(missing_ok=True)
                raise InvalidMediaUploadException(f"Upload exceeds the {max_bytes}-byte limit")
            digest.update(chunk)
            target.write(chunk)
    return digest.hexdigest()


def create_media_upload_router(max_upload_bytes: int = 512 * 1024 * 1024) -> APIRouter:
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
            The binary media file. Spooled to disk by the multipart parser, then
            copied in chunks; never held whole in memory.
        object
            A JSON-serialised ActivityStreams object shell (may also be a
            ``Create`` wrapper).  The server assigns ``id`` and ``url``.

        Returns ``201 Created`` with a ``Location`` header pointing to the
        newly created object's URI.
        """
        if not principal:
            raise HTTPException(HTTPStatus.UNAUTHORIZED, "Authentication required")

        # A declared length is a claim, not a promise: reject early on it, and
        # again while copying (see _copy_stream_to_file).
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_upload_bytes:
            raise HTTPException(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                detail=f"Upload exceeds the {max_upload_bytes}-byte limit",
            )

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

        if file_field is None or isinstance(file_field, str):
            raise HTTPException(
                HTTPStatus.BAD_REQUEST,
                detail="Both 'file' and 'object' form fields are required",
            )

        actor_uri = principal.uri
        username = actor_uri.rstrip("/").rsplit("/", 1)[-1]
        media_dir = request.state.tenant.files / username / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(getattr(file_field, "filename", "") or "").suffix

        handle, scratch_name = tempfile.mkstemp(dir=media_dir, suffix=".part")
        os.close(handle)
        scratch = Path(scratch_name)
        try:
            digest = _copy_stream_to_file(file_field.file, scratch, max_bytes=max_upload_bytes)
            stored = media_dir / f"{digest}{suffix}"
            os.replace(scratch, stored)
        except InvalidMediaUploadException as e:
            raise HTTPException(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, detail=str(e)) from None
        finally:
            scratch.unlink(missing_ok=True)

        try:
            media_object = await service.process_upload(
                tenant=request.state.tenant,
                principal=principal,
                object_text=object_text,
                file_data=None,
                content_type=getattr(file_field, "content_type", None)
                or "application/octet-stream",
                filename=getattr(file_field, "filename", None) or "upload",
                stored_path=stored,
            )
        except NotAuthorizedException as e:
            raise HTTPException(HTTPStatus.FORBIDDEN, detail=str(e))
        except InvalidMediaUploadException as e:
            stored.unlink(missing_ok=True)
            raise HTTPException(HTTPStatus.BAD_REQUEST, detail=str(e))

        return JSONResponse(
            status_code=HTTPStatus.CREATED.value,
            headers={"Location": str(media_object["id"])},
            content=media_object,
        )

    @router.api_route(_SERVE_PATH, methods=["GET", "HEAD"])
    async def serve_media(request: Request, username: str, filename: str) -> Response:
        """Serve a previously uploaded media file.

        The service accepts only a bare username and filename, and refuses any
        path that resolves outside the tenant's files directory — which is what
        a symlink pointing elsewhere would otherwise get past.

        ``HEAD`` is served as well as ``GET`` because peers probe before they
        download: Pixelfed's avatar fetcher asks for the mime type and length
        with a HEAD and skips the file entirely when that probe does not come
        back 200. The body is dropped by ``FileResponse`` for HEAD, so the
        headers are identical to the GET they precede.
        """
        try:
            file_path = service.resolve_media_path(request.state.tenant, username, filename)
        except InvalidMediaPathException as e:
            raise HTTPException(HTTPStatus.BAD_REQUEST, detail=str(e))
        except MediaNotFoundException as e:
            raise HTTPException(HTTPStatus.NOT_FOUND, detail=str(e))

        return FileResponse(file_path)

    return router
