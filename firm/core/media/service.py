"""
Media upload service implementing the ActivityPub MediaUpload protocol.
https://www.w3.org/wiki/SocialCG/ActivityPub/MediaUpload

This module is HTTP-framework-independent.  The FastAPI adapter lives in
``firm.server.media_upload``.
"""

import hashlib
from pathlib import Path

from firm.core.interfaces import Identity, JSONObject, Tenant
from firm.core.services.exception import ServiceException


class NotAuthorizedException(ServiceException):
    def __init__(self, reason: str | None = None):
        super().__init__(reason or "Not authorized")

    @property
    def reason(self) -> str:
        return self.args[0]


class InvalidMediaUploadException(ServiceException):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class MediaUploadService:
    """Implements the server-side media-upload logic described in the
    ActivityPub MediaUpload protocol.

    Responsibilities
    ----------------
    * Validate that the request is authenticated.
    * Persist the uploaded bytes under the tenant's per-actor files directory.
    * Finalise the AS2 object shell: assign a permanent ``id``, set ``url``
      to the publicly accessible media URL, and ensure ``attributedTo`` is
      set.
    * Store the finished object in the tenant's public resource store.
    * Return the new object URI (used by the HTTP layer as the ``Location``
      response header value).
    """

    async def process_upload(
        self,
        tenant: Tenant,
        principal: Identity | None,
        object_shell: JSONObject,
        file_data: bytes,
        content_type: str,
        filename: str,
    ) -> JSONObject:
        """Process a media upload and return the URI of the created object.

        Parameters
        ----------
        tenant:
            The tenant handling the request.
        principal:
            The authenticated actor performing the upload.  ``None`` causes
            a :exc:`NotAuthorizedException` to be raised.
        object_shell:
            The AS2 object (or ``Create`` wrapper around one) submitted by
            the client.  May lack ``id``, ``url``, and ``attributedTo``.
        file_data:
            Raw bytes of the uploaded file.
        content_type:
            MIME type reported by the client for the uploaded file.
        filename:
            Original filename provided by the client.
        """
        if principal is None:
            raise NotAuthorizedException("Authentication required")

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
            inner["@context"] = "https://www.w3.org/ns/activitystreams"

        await tenant.public_store.put(inner)
        return inner
