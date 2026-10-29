"""Serve the JSON-LD @context documents from tests/data.

`pyld` fetches context documents at runtime. Every RDF test would need w3.org, which
answers this host with a Cloudflare challenge, so the tests were red for a reason that had
nothing to do with the code under test. The contexts are bundled instead: the loader is
replaced, not mocked per call, and anything not bundled fails loudly.
"""

import json
import pathlib
from typing import Any, Literal

import pytest

from firm.ld import jsonld_utils

BUNDLED = {
    "https://www.w3.org/ns/activitystreams": pathlib.Path(__file__).parent.parent
    / "data"
    / "activitystreams.jsonld",
    "https://w3c-ccg.github.io/security-vocab/contexts/security-v1.jsonld": pathlib.Path(
        __file__
    ).parent.parent
    / "data"
    / "security-v1.jsonld",
}


class _Response:
    def __init__(self, document: Any) -> None:
        self._document = document
        self.is_success = True

    def json(self) -> Any:
        return self._document


class _Client:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *exc: Any) -> Literal[False]:
        return False

    def get(self, url: str, **kwargs: Any) -> _Response:
        path = BUNDLED.get(url)
        if path is None:
            path = BUNDLED.get(url.split("#", 1)[0])
        if path is None:
            raise AssertionError(f"test tried to fetch {url}; bundle it in tests/data")
        return _Response(json.loads(path.read_text(encoding="utf-8")))


@pytest.fixture(autouse=True)
def bundled_jsonld_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jsonld_utils.httpx, "Client", _Client)
