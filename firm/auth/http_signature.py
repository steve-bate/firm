import base64
import logging
from email.utils import formatdate
from hashlib import sha256
from typing import Mapping, Sequence, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.backends import default_backend as crypto_default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization as crypto_serialization
from cryptography.hazmat.primitives.asymmetric import dh, padding, x448, x25519

from firm.interfaces import (
    APActor,
    HttpRequest,
    HttpRequestSigner,
    JSONObject,
    Principal,
)

log = logging.getLogger(__name__)


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
                    request.headers["Date"] = formatdate(
                        timeval=None, localtime=False, usegmt=True
                    )
                elif header.lower() == "digest":
                    if body := request.content():
                        request.headers["Digest"] = "SHA-256=" + base64.b64encode(
                            sha256(body).digest()
                        ).decode("utf-8")
                elif header.lower() == "host":
                    request.headers["Host"] = request.url.hostname


class HttpSigAuthenticator(HttpSignatureMixin):
    def __init__(self, headers: Sequence[str] | None = None):
        HttpSignatureMixin.__init__(self, headers)

    async def authenticate(self, request: HttpRequest):
        if "Signature" not in request.headers:
            return None

        signature_header = request.headers.get("Signature")
        if not isinstance(signature_header, str):
            return None

        signature_fields = self.get_signature_fields(signature_header)
        signature = base64.b64decode(signature_fields["signature"].encode("utf-8"))

        headers = signature_fields["headers"].split(" ")
        signed_string, headers_text = self.construct_signature_data(request, headers)

        # if headers_text != " ".join(self._headers):
        #     raise ValueError("Headers listed in signature mismatch with request")

        store = request.app.state.store

        key_id = signature_fields["keyId"]
        key = await store.get(key_id)

        # This is a hack because of the #main-key fragment
        if key is None:
            actor = await store.get(key_id.replace("#main-key", ""))
            if actor:
                if isinstance(actor["publicKey"], Mapping):
                    key = cast(JSONObject, actor["publicKey"])

        if key is not None and "publicKey" in key:
            # Not really the key, but the actor and the key is a fragment
            if isinstance(key, Mapping):
                key = cast(JSONObject, key)
            key = cast(JSONObject, key["publicKey"])

        if isinstance(key, Mapping) and "publicKeyPem" in key:
            public_key_pem = str(key["publicKeyPem"])
        else:
            raise ValueError(f"Invalid public key: {key=}")

        public_key = crypto_serialization.load_pem_public_key(
            public_key_pem.encode("utf-8"), backend=crypto_default_backend()
        )

        try:
            if not isinstance(
                public_key, (dh.DHPublicKey, x25519.X25519PublicKey, x448.X448PublicKey)
            ):
                public_key.verify(
                    signature,
                    signed_string.encode("utf-8"),
                    padding.PKCS1v15(),  # type: ignore
                    hashes.SHA256(),  # type: ignore
                )
            else:
                return None
        except InvalidSignature:
            return None

        # if "digest" in headers and conn.content is not None:
        #     body = await conn.content
        #     digest = "SHA-256=" + base64.b64encode(sha256(body).digest()).decode(
        #         "utf-8"
        #     )
        #     if conn.headers["Digest"] != digest:
        #         raise ValueError("Digest of body is invalid")

        principal_uri = str(key["owner"])
        principal = cast(APActor | None, await store.get(principal_uri))

        if principal:
            return Principal(principal)
        else:
            raise ValueError(f"Unknown user: {principal_uri}")

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
