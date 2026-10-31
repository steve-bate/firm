"""Naming helpers shared by the store and its sidecar files."""

import hashlib


def uri_hash(uri: str) -> str:
    """The md5 of a URI, used as the file name for that URI's data."""
    digest = hashlib.md5()
    digest.update(uri.encode())
    return digest.hexdigest()
