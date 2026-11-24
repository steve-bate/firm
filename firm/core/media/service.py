"""
Media upload service implementing the ActivityPub MediaUpload protocol.
https://www.w3.org/wiki/SocialCG/ActivityPub/MediaUpload

This module is HTTP-framework-independent: every decision the media endpoints
make lives here. The FastAPI adapter is ``firm.server.media``.
"""

import hashlib
import json
from pathlib import Path

from firm.core.interfaces import AuthorizationService, Identity, JSONObject, Tenant
from firm.core.services.activitypub.support import generate_id
from firm.core.services.exception import ServiceException
from firm.core.util import AS2_CONTEXT, get_id


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


def media_url_for(tenant: Tenant, username: str, filename: str) -> str:
    """Where a stored file is served from.

    One builder for the write and the read: the URL ``process_upload`` records on the
    object is the URL the serve route is asked for, and a second spelling of it here
    would silently make every file unreadable.
    """
    return f"{tenant.prefix}/actors/{username}/media/{filename}"


class MediaUploadService:
    """Implements the server-side media-upload logic described in the
    ActivityPub MediaUpload protocol.

    Responsibilities
    ----------------
    * Validate the request: both form parts present, the object field parsing
      to a JSON object.
    * Validate that the request is authenticated.
    * Persist the uploaded bytes under the tenant's per-actor files directory.
    * Finalise the AS2 object shell: assign an ``id`` in the actor's object
      namespace, set ``url`` to the publicly accessible media URL, and ensure
      ``attributedTo`` is set.
    * Store the finished object in the tenant's public resource store.
    * Return the new object, whose ``id`` the HTTP layer uses as the
      ``Location`` response header value.
    * Resolve the file named by a serve request, refusing any path that escapes
      the tenant's files directory.
    """

    def parse_upload(
        self,
        *,
        object_text: str | None,
        file_data: bytes | None = None,
        stored_path: Path | None = None,
    ) -> JSONObject:
        """The AS2 object shell an upload request carries.

        Raises :exc:`InvalidMediaUploadException` for a missing form part, a
        body that is not JSON, and a body that is not a JSON object.
        """
        if (file_data is None and stored_path is None) or object_text is None:
            raise InvalidMediaUploadException("Both 'file' and 'object' form fields are required")
        try:
            shell = json.loads(object_text)
        except (json.JSONDecodeError, ValueError):
            raise InvalidMediaUploadException("The 'object' field must be valid JSON") from None
        if not isinstance(shell, dict):
            raise InvalidMediaUploadException("The 'object' field must be a JSON object")
        return shell

    async def is_readable(
        self,
        tenant: Tenant,
        authorizer: AuthorizationService,
        principal: Identity | None,
        media_url: str,
    ) -> bool:
        """Whether ``principal`` may read the bytes served at ``media_url``.

        A file is content-addressed and an object is not, so one file can be named by
        several objects: the image a direct message carries and the same image attached
        to a public post are two objects over one URL. The bytes are as readable as the
        most readable object that names them, and the decision is the ordinary document
        policy — a public object allows any reader, an addressed one allows its
        recipients, and an upload nothing public names answers only to its owner.

        A post that attaches the object decides as well as the object itself. An upload
        made before uploads carried an audience has no recipients of its own, so reading
        only the object would take every picture already published off the air; the note
        that shows it is the object whose audience the reader can see.

        A request that names no object at all is refused: a file on disk with nothing in
        the store referring to it is not addressable content, and this is also the state
        an interrupted upload leaves behind.
        """
        for document in await tenant.public_store.query({"url": media_url}):
            decision = await authorizer.is_get_authorized(tenant, principal, document)
            if decision.authorized:
                return True
            media_id = get_id(document)
            if not media_id:
                continue
            for referrer in await tenant.public_store.query({"attachment": media_id}):
                readable = await authorizer.is_get_authorized(tenant, principal, referrer)
                if readable.authorized:
                    return True
        return False

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
        stored_path: Path | None = None,
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

        object_shell = self.parse_upload(
            object_text=object_text, file_data=file_data, stored_path=stored_path
        )

        actor_uri = principal.uri
        # Use the last path segment of the actor URI as the storage directory.
        username = actor_uri.rstrip("/").rsplit("/", 1)[-1]

        # --- Persist the uploaded file ---
        if stored_path is not None:
            # The HTTP layer already streamed and content-addressed the file.
            stored_filename = stored_path.name
        else:
            assert file_data is not None
            file_id = hashlib.sha256(file_data).hexdigest()
            # Keep the original extension, but nothing else from the filename.
            suffix = Path(filename).suffix if filename else ""
            stored_filename = f"{file_id}{suffix}"

            actor_media_dir = tenant.files / username / "media"
            actor_media_dir.mkdir(parents=True, exist_ok=True)
            (actor_media_dir / stored_filename).write_bytes(file_data)

        # Publicly accessible URL that the server will serve the file from.
        media_url = media_url_for(tenant, username, stored_filename)

        # --- Finalise the AS2 object ---
        # If the client wrapped the object in a Create activity, unwrap it.
        inner: JSONObject = dict(object_shell)
        if inner.get("type") == "Create" and isinstance(inner.get("object"), dict):
            inner = dict(inner["object"])  # type: ignore[arg-type]

        # The id belongs in the object namespace, not the media one: the file route
        # serves bytes only, so an id under it would never reach the object stored
        # here. ``generate_id`` is the same id shape every other created object gets.
        inner["id"] = generate_id(principal.uri, inner)
        inner["url"] = media_url
        if "attributedTo" not in inner:
            inner["attributedTo"] = actor_uri
        if "@context" not in inner:
            inner["@context"] = AS2_CONTEXT

        await tenant.public_store.put(inner)
        return inner
