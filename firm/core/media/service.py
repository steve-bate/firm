"""
Media upload service implementing the ActivityPub MediaUpload protocol.
https://www.w3.org/wiki/SocialCG/ActivityPub/MediaUpload

This module is HTTP-framework-independent: every decision the media endpoints
make lives here. The FastAPI adapter is ``firm.server.media``.
"""

import hashlib
import json
from pathlib import Path

from firm.core.interfaces import Identity, JSONObject, Tenant
from firm.core.services.exception import ServiceException
from firm.core.util import AS2_CONTEXT


class NotAuthorizedException(ServiceException):
    def __init__(self, reason: str | None = None):
        super().__init__(reason or "Not authorized")

    @property
    def reason(self) -> str:
        return self.args[0]


class InvalidMediaUploadException(ServiceException):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class InvalidMediaPathException(ServiceException):
    """A media path that leaves the tenant's files directory or names no file."""

    def __init__(self, reason: str = "Invalid path"):
        super().__init__(reason)


class MediaNotFoundException(ServiceException):
    def __init__(self) -> None:
        super().__init__("File not found")


class MediaUploadService:
    """Implements the server-side media-upload logic described in the
    ActivityPub MediaUpload protocol.

    Responsibilities
    ----------------
    * Validate the request: both form parts present, the object field parsing
      to a JSON object.
    * Validate that the request is authenticated.
    * Persist the uploaded bytes under the tenant's per-actor files directory.
    * Finalise the AS2 object shell: assign a permanent ``id``, set ``url``
      to the publicly accessible media URL, and ensure ``attributedTo`` is
      set.
    * Store the finished object in the tenant's public resource store.
    * Return the new object, whose ``id`` the HTTP layer uses as the
      ``Location`` response header value.
    * Resolve the file named by a serve request, refusing any path that escapes
      the tenant's files directory.
    """

    def parse_upload(self, *, object_text: str | None, file_data: bytes | None) -> JSONObject:
        """The AS2 object shell an upload request carries.

        Raises :exc:`InvalidMediaUploadException` for a missing form part, a
        body that is not JSON, and a body that is not a JSON object.
        """
        if file_data is None or object_text is None:
            raise InvalidMediaUploadException("Both 'file' and 'object' form fields are required")
        try:
            shell = json.loads(object_text)
        except (json.JSONDecodeError, ValueError):
            raise InvalidMediaUploadException("The 'object' field must be valid JSON") from None
        if not isinstance(shell, dict):
            raise InvalidMediaUploadException("The 'object' field must be a JSON object")
        return shell

    def resolve_media_path(self, tenant: Tenant, username: str, filename: str) -> Path:
        """The stored file one actor's media name refers to.

        Path traversal is prevented by accepting only a bare username and a bare
        filename (no separators, no parent segments), and by requiring the
        resolved path to stay inside the tenant's files directory — which is
        what a symlink pointing elsewhere would otherwise get past.

        Raises :exc:`InvalidMediaPathException` for a rejected path and
        :exc:`MediaNotFoundException` when nothing is stored under it.
        """
        safe_filename = filename.replace("\\", "/")
        if ".." in safe_filename or "/" in safe_filename:
            raise InvalidMediaPathException("Invalid filename")
        safe_username = username.replace("\\", "/")
        if ".." in safe_username or "/" in safe_username:
            raise InvalidMediaPathException("Invalid username")

        file_path = (tenant.files / safe_username / "media" / safe_filename).resolve()
        try:
            file_path.relative_to(tenant.files.resolve())
        except ValueError:
            raise InvalidMediaPathException("Invalid path") from None
        if not file_path.is_file():
            raise MediaNotFoundException()
        return file_path

    async def process_upload(
        self,
        tenant: Tenant,
        principal: Identity | None,
        object_text: str | None,
        file_data: bytes | None,
        content_type: str,
        filename: str,
    ) -> JSONObject:
        """Process a media upload and return the created object.

        Parameters
        ----------
        tenant:
            The tenant handling the request.
        principal:
            The authenticated actor performing the upload.  ``None`` causes
            a :exc:`NotAuthorizedException` to be raised.
        object_text:
            The ``object`` form part as submitted: the AS2 object (or
            ``Create`` wrapper around one).  May lack ``id``, ``url``, and
            ``attributedTo``.
        file_data:
            Raw bytes of the uploaded file.
        content_type:
            MIME type reported by the client for the uploaded file.
        filename:
            Original filename provided by the client.
        """
        if principal is None:
            raise NotAuthorizedException("Authentication required")

        object_shell = self.parse_upload(object_text=object_text, file_data=file_data)
        assert file_data is not None

        actor_uri = principal.uri
        # Use the last path segment of the actor URI as the storage directory.
        username = actor_uri.rstrip("/").rsplit("/", 1)[-1]

        # --- Persist the uploaded file ---
        file_id = hashlib.sha256(file_data).hexdigest()
        # Keep the original extension, but nothing else from the filename.
        suffix = Path(filename).suffix if filename else ""
        stored_filename = f"{file_id}{suffix}"

        actor_media_dir = tenant.files / username / "media"
        actor_media_dir.mkdir(parents=True, exist_ok=True)
        (actor_media_dir / stored_filename).write_bytes(file_data)

        # Publicly accessible URL that the server will serve the file from.
        media_url = f"{tenant.prefix}/actors/{username}/media/{stored_filename}"

        # --- Finalise the AS2 object ---
        # If the client wrapped the object in a Create activity, unwrap it.
        inner: JSONObject = dict(object_shell)
        if inner.get("type") == "Create" and isinstance(inner.get("object"), dict):
            inner = dict(inner["object"])  # type: ignore[arg-type]

        inner["id"] = f"{tenant.prefix}/actors/{username}/media/{file_id}"
        inner["url"] = media_url
        if "attributedTo" not in inner:
            inner["attributedTo"] = actor_uri
        if "@context" not in inner:
            inner["@context"] = AS2_CONTEXT

        await tenant.public_store.put(inner)
        return inner
