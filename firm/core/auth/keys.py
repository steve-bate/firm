from dataclasses import dataclass

from cryptography.hazmat.backends import default_backend as crypto_default_backend
from cryptography.hazmat.primitives import serialization as crypto_serialization
from cryptography.hazmat.primitives.asymmetric import dh, rsa, x448, x25519


@dataclass
class KeyPair:
    public: str
    private: str


def create_key_pair() -> KeyPair:
    pair = rsa.generate_private_key(
        backend=crypto_default_backend(), public_exponent=65537, key_size=2048
    )
    return KeyPair(
        public=pair.public_key()
        .public_bytes(
            crypto_serialization.Encoding.PEM,
            crypto_serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
        private=pair.private_bytes(
            crypto_serialization.Encoding.PEM,
            crypto_serialization.PrivateFormat.PKCS8,
            # FIXME support encryption with a configured passphrase
            crypto_serialization.NoEncryption(),
        ).decode(),
    )


def public_key_from_private(private_key_pem: str) -> str:
    """The public half of a private key, in the PEM form an actor document publishes.

    A server signs with the private key it stores and peers verify with the public key it
    publishes, so the published key must be derived from that private key rather than
    generated beside it: two independently generated halves never match, and every peer
    with a real verifier drops the result.
    """
    if not private_key_pem:
        raise ValueError("No private key to derive a public key from")
    private_key = crypto_serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"), password=None, backend=crypto_default_backend()
    )
    if isinstance(private_key, (dh.DHPrivateKey, x25519.X25519PrivateKey, x448.X448PrivateKey)):
        raise ValueError("Unsupported private key type")
    return (
        private_key.public_key()  # type: ignore[union-attr]
        .public_bytes(
            crypto_serialization.Encoding.PEM,
            crypto_serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
