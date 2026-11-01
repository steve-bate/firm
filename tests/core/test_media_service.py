"""Behaviour of the media service, exercised without an HTTP client.

The traversal and symlink cases had no coverage at all while they lived in the
route; they are the reason this logic moved.
"""

import hashlib
from pathlib import Path
from typing import cast

import pytest

from firm.core.interfaces import APActor, JSONObject, Principal, Tenant
from firm.core.media.service import (
    InvalidMediaPathException,
    InvalidMediaUploadException,
    MediaNotFoundException,
    MediaUploadService,
)
from firm.core.store.memory import MemoryResourceStore


@pytest.fixture
def tenant(tmp_path) -> Tenant:
    return Tenant(
        "http://tenant1.test",
        MemoryResourceStore(),
        MemoryResourceStore(),
        tmp_path,
    )


@pytest.fixture
def service() -> MediaUploadService:
    return MediaUploadService()


@pytest.fixture
def principal(tenant: Tenant) -> Principal:
    return Principal(cast(APActor, {"id": "http://tenant1.test/user1", "type": "Person"}), tenant)


async def _upload(
    service: MediaUploadService,
    tenant: Tenant,
    principal: Principal,
    *,
    object_text: str | None = '{"type": "Image"}',
    file_data: bytes | None = b"png",
) -> JSONObject:
    return await service.process_upload(
        tenant,
        principal,
        object_text=object_text,
        file_data=file_data,
        content_type="image/png",
        filename="photo.png",
    )


async def test_upload_requires_both_form_parts(
    service: MediaUploadService, tenant: Tenant, principal: Principal
) -> None:
    with pytest.raises(InvalidMediaUploadException, match="Both 'file' and 'object'"):
        await _upload(service, tenant, principal, file_data=None)
    with pytest.raises(InvalidMediaUploadException, match="Both 'file' and 'object'"):
        await _upload(service, tenant, principal, object_text=None)


async def test_upload_rejects_a_body_that_is_not_json(
    service: MediaUploadService, tenant: Tenant, principal: Principal
) -> None:
    with pytest.raises(InvalidMediaUploadException, match="must be valid JSON"):
        await _upload(service, tenant, principal, object_text="{")


async def test_upload_rejects_json_that_is_not_an_object(
    service: MediaUploadService, tenant: Tenant, principal: Principal
) -> None:
    with pytest.raises(InvalidMediaUploadException, match="must be a JSON object"):
        await _upload(service, tenant, principal, object_text="[]")


async def test_upload_stores_the_bytes_and_finalises_the_object(
    service: MediaUploadService, tenant: Tenant, principal: Principal
) -> None:
    stored = await _upload(service, tenant, principal)

    digest = hashlib.sha256(b"png").hexdigest()
    # The id names the object; only ``url`` names the file. An id under the media
    # route would be claimed by it and never reach the stored object.
    object_id = str(stored["id"])
    assert object_id.startswith("http://tenant1.test/user1/image/")
    assert object_id != f"http://tenant1.test/actors/user1/media/{digest}"
    assert stored["url"] == f"http://tenant1.test/actors/user1/media/{digest}.png"
    assert stored["attributedTo"] == "http://tenant1.test/user1", "the uploader is attributed"
    assert (Path(tenant.files) / "user1" / "media" / f"{digest}.png").read_bytes() == b"png"
    assert await tenant.public_store.get(str(stored["id"])) == stored


def test_serve_returns_a_file_inside_the_tenant(
    service: MediaUploadService, tenant: Tenant
) -> None:
    media = Path(tenant.files) / "user1" / "media"
    media.mkdir(parents=True)
    (media / "file.png").write_bytes(b"png")

    assert service.resolve_media_path(tenant, "user1", "file.png") == media / "file.png"


@pytest.mark.parametrize("filename", ["../file.png", "sub/file.png", "..", "a\\b"])
def test_serve_rejects_a_filename_that_is_not_bare(
    service: MediaUploadService, tenant: Tenant, filename: str
) -> None:
    (Path(tenant.files) / "user1" / "media").mkdir(parents=True)

    with pytest.raises(InvalidMediaPathException):
        service.resolve_media_path(tenant, "user1", filename)


def test_serve_rejects_a_username_that_is_not_bare(
    service: MediaUploadService, tenant: Tenant
) -> None:
    (Path(tenant.files) / "user1" / "media").mkdir(parents=True)

    with pytest.raises(InvalidMediaPathException, match="Invalid username"):
        service.resolve_media_path(tenant, "../user2", "file.png")


def test_serve_rejects_a_symlink_that_leaves_the_files_directory(
    service: MediaUploadService, tenant: Tenant, tmp_path_factory
) -> None:
    media = Path(tenant.files) / "user1" / "media"
    media.mkdir(parents=True)
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_bytes(b"secret")
    (media / "link.png").symlink_to(outside)

    with pytest.raises(InvalidMediaPathException, match="Invalid path"):
        service.resolve_media_path(tenant, "user1", "link.png")


def test_serve_reports_a_missing_file(service: MediaUploadService, tenant: Tenant) -> None:
    (Path(tenant.files) / "user1" / "media").mkdir(parents=True)

    with pytest.raises(MediaNotFoundException):
        service.resolve_media_path(tenant, "user1", "absent.png")
