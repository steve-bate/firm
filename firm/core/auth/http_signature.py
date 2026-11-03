import base64
import hmac
import logging
from email.utils import formatdate
from hashlib import sha256
from typing import Mapping, Sequence, cast
from urllib.parse import urldefrag

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.backends import default_backend as crypto_default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization as crypto_serialization
from cryptography.hazmat.primitives.asymmetric import dh, padding, x448, x25519

from firm.core.interfaces import (
    APActor,
    HttpRequest,
    HttpRequestSigner,
    JSONObject,
    Principal,
    Tenant,
)
from firm.core.resolver import ResourceResolver
from firm.core.util import get_id, is_urn

log = logging.getLogger(__name__)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """One header by case-insensitive name: a stub's dict is not a Starlette Headers."""
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


class HttpSignatureMixin:
    DEFAULT_HEADERS = ["(request-target)", "host", "date", "digest"]

    def __init__(self, signature_headers: Sequence[str] | None):
        self._headers = signature_headers or self.DEFAULT_HEADERS

    def construct_signature_data(
        self, request: HttpRequest, headers: list[str] | None = None
    ) -> tuple[str, str]:
        signature_data = []
        used_headers = []
        for header in headers or self._headers:
            # FIXME support created and expires pseudo-headers
            if header.lower() == "(request-target)":
                method = request.method.lower()
                path = request.url.path
                signature_data.append(f"(request-target): {method} {path}")
                used_headers.append("(request-target)")
            elif header in request.headers:
                name = header.lower()
                value = request.headers[header]
                signature_data.append(f"{name}: {value}")
                used_headers.append(name)
            # FIXME This needs to be async
            elif header == "digest" and not request.content():
                continue
            else:
                raise KeyError("Header %s not found", header)
        signed_string = "\n".join(signature_data)
        headers_text = " ".join(used_headers)
        return signed_string, headers_text

    def synthesize_headers(self, request: HttpRequest) -> None:
        for header in self._headers:
            if header not in request.headers:
                if header.lower() == "date":
                    request.headers["Date"] = formatdate(timeval=None, localtime=False, usegmt=True)
                elif header.lower() == "digest":
                    if body := request.content():
                        request.headers["Digest"] = "SHA-256=" + base64.b64encode(
                            sha256(body).digest()
                        ).decode("utf-8")
                elif header.lower() == "host":
                    request.headers["Host"] = request.url.hostname


class HttpSigAuthenticator(HttpSignatureMixin):
    """Verify a request signed by a local or a remote actor.

    Keys are resolved through the resolver: a URI owned by a local tenant is read from
    that tenant's store, and a remote one is fetched and cached by the remote provider.
    A remote actor document is never copied into the receiving tenant's store.
    """

    def __init__(
        self,
        headers: Sequence[str] | None = None,
        *,
        resolver: ResourceResolver | None = None,
    ):
        HttpSignatureMixin.__init__(self, headers)
        self._resolver = resolver

    async def authenticate(self, request: HttpRequest):
        signature_header = request.headers.get("Signature")
        if not isinstance(signature_header, str):
            return None

        try:
            signature_fields = self.get_signature_fields(signature_header)
            signature = base64.b64decode(signature_fields["signature"].encode("utf-8"))
            header_names = signature_fields.get("headers", " ".join(self._headers)).split()
            signed_string, _headers_text = self.construct_signature_data(request, header_names)
            key_id = signature_fields["keyId"]
        except (KeyError, ValueError):
            log.info("Inbound signature header is unreadable")
            return None

        tenant = request.state.tenant
        resolved = await self._load_key(key_id, tenant)
        if resolved is None:
            log.info("Inbound signature names no usable key: %s", key_id)
            return None
        key, key_document, remote = resolved
        public_key_pem = key.get("publicKeyPem")
        if not isinstance(public_key_pem, str) or not public_key_pem:
            return None

        try:
            public_key = crypto_serialization.load_pem_public_key(
                public_key_pem.encode("utf-8"), backend=crypto_default_backend()
            )
        except (ValueError, TypeError):
            log.info("Inbound signature key is not a PEM public key: %s", key_id)
            return None
        if isinstance(public_key, (dh.DHPublicKey, x25519.X25519PublicKey, x448.X448PublicKey)):
            return None
        try:
            public_key.verify(  # type: ignore[attr-defined]
                signature,
                signed_string.encode("utf-8"),
                padding.PKCS1v15(),  # type: ignore
                hashes.SHA256(),  # type: ignore
            )
        except (InvalidSignature, ValueError, TypeError):
            return None

        body = await request.body()
        if body:
            if "digest" not in [name.lower() for name in header_names]:
                log.info("Inbound signature does not cover the digest")
                return None
            if not self._digest_matches(body, _header(request.headers, "digest")):
                log.info("Inbound signature digest does not match the body")
                return None

        key_owner = key.get("owner")
        if not isinstance(key_owner, str) or not key_owner:
            return None
        principal = await self._load_owner(
            key_owner, key_id, public_key_pem, key_document, remote, tenant
        )
        if principal is None:
            log.info("Inbound signature key owner is not usable: %s", key_owner)
            return None
        return Principal(cast(APActor, principal), tenant)

    async def _load_key(
        self, key_id: str, tenant: Tenant
    ) -> tuple[JSONObject, JSONObject, bool] | None:
        """The key named by keyId, the document it came from, and whether that is remote."""
        remote = (
            self._resolver is not None
            and self._resolver.owner(key_id) is None
            and not is_urn(key_id)
        )
        document = await self._load(key_id, tenant)
        if document is None:
            base = urldefrag(key_id)[0]
            if base != key_id:
                document = await self._load(base, tenant)
        if document is None:
            return None
        key = self._find_key(document, key_id)
        if key is None:
            return None
        return key, document, remote

    async def _load(self, uri: str, tenant: Tenant) -> JSONObject | None:
        """Read a document from whichever store is authoritative for its URI.

        With no resolver this keeps the historical behaviour: the receiving tenant's
        store only.
        """
        if self._resolver is None:
            return await tenant.public_store.get(uri)
        owner = self._resolver.owner(uri)
        if owner is not None:
            return await owner.public_store.get(uri)
        if is_urn(uri):
            return await tenant.public_store.get(uri)
        return await self._resolver.fetch(uri)

    @staticmethod
    def _find_key(document: JSONObject, key_id: str) -> JSONObject | None:
        """The key document itself, or the ``publicKey`` entry a keyId names."""
        if "publicKeyPem" in document:
            return document
        public_key = document.get("publicKey")
        if isinstance(public_key, Mapping):
            candidates = [cast(JSONObject, public_key)]
        elif isinstance(public_key, list):
            candidates = [
                cast(JSONObject, item) for item in public_key if isinstance(item, Mapping)
            ]
        else:
            return None
        for candidate in candidates:
            if candidate.get("id") == key_id:
                return candidate
        # Some servers sign with the actor URI rather than its key fragment; with a
        # single key on the document there is only one key that keyId can mean.
        if urldefrag(key_id)[0] == get_id(document) and len(candidates) == 1:
            return candidates[0]
        return None

    async def _load_owner(
        self,
        key_owner: str,
        key_id: str,
        public_key_pem: str,
        key_document: JSONObject,
        remote: bool,
        tenant: Tenant,
    ) -> JSONObject | None:
        """The actor a key belongs to.

        Locally held documents are authoritative. A document fetched from another host
        is trusted only when the actor it names owns that key: either the key document
        *is* the actor, or the actor document names this keyId with the same PEM.
        Otherwise a key on any host could claim any owner.
        """
        if remote:
            if get_id(key_document) == key_owner:
                return key_document
            owner = await self._load(key_owner, tenant)
            if owner is None:
                return None
            vouching = self._find_key(owner, key_id)
            if vouching is None or vouching.get("publicKeyPem") != public_key_pem:
                return None
            return owner
        principal = await self._load(key_owner, tenant)
        if principal is None and get_id(key_document) == key_owner:
            return key_document
        return principal

    @staticmethod
    def _digest_matches(body: bytes, digest_header: object) -> bool:
        """Whether a ``Digest`` header carries the SHA-256 of this body."""
        if not isinstance(digest_header, str):
            return False
        algorithm, _, value = digest_header.strip().partition("=")
        if algorithm.upper().replace("-", "") != "SHA256":
            return False
        expected = base64.b64encode(sha256(body).digest()).decode("utf-8")
        return hmac.compare_digest(value.strip(), expected)

    @staticmethod
    def get_signature_fields(signature_header: str) -> dict[str, str]:
        signature_fields = {}
        for field in signature_header.split(","):
            name, value = field.split("=", 1)
            if name in signature_fields:
                raise KeyError(f"Duplicate field {name} in signature")
            signature_fields[name] = value.strip('"')
        return signature_fields


class HttpSignatureAuth(HttpSignatureMixin, HttpRequestSigner):
    def __init__(
        self,
        key_id: str,
        private_key: str,
        headers: Sequence[str] | None = None,
    ):
        HttpSignatureMixin.__init__(self, headers)
        self._key_id = key_id
        self._private_key = crypto_serialization.load_pem_private_key(
            private_key.encode("utf-8"),
            password=None,
            backend=crypto_default_backend(),
        )

    def sign(self, request: HttpRequest) -> None:
        if not self._private_key:
            raise Exception("Private key unknown. Skipping signature.")

        if isinstance(
            self._private_key,
            (dh.DHPrivateKey, x25519.X25519PrivateKey, x448.X448PrivateKey),
        ):
            raise ValueError("Unsupported private key type")

        self.synthesize_headers(request)
        signed_string, headers_text = self.construct_signature_data(request)

        signature = base64.b64encode(
            self._private_key.sign(
                signed_string.encode("utf-8"),
                padding.PKCS1v15(),  # type: ignore
                hashes.SHA256(),  # type: ignore
            )
        ).decode("utf-8")

        signature_fields = [
            f'keyId="{self._key_id}"',
            'algorithm="rsa-sha256"',
            f'headers="{headers_text}"',
            f'signature="{signature}"',
        ]

        signature_header = ",".join(signature_fields)

        request.headers["Signature"] = signature_header
