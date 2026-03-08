from pathlib import Path

import pytest
from jsonschema import ValidationError

from firm.jsonschema.validation import create_validator


@pytest.fixture(scope="session")
def validator():
    return create_validator(
        root_schema="schema:activity",
        schema_dirs=[Path(__file__).parent / "schemas"],
    )


VALIDATION_TEST_CASES = [
    pytest.param({"type": 123}, "fail", id="Wrong type type - int"),
    pytest.param({"type": "Bogus"}, "fail", id="Invalid type"),
    pytest.param({"type": "Follow"}, "fail", id="No object"),
    pytest.param(
        {"type": ["Follow", "Reject"], "object": "http://server.test"},
        "pass",
        id="Multiple types allowed",
    ),
    pytest.param(
        {"type": "Follow", "object": {"id": "http://server.test"}},
        "pass",
        id="Accepts objects for 'object'",
    ),
    pytest.param({"type": "Follow", "object": "@foo@bar.test"}, "fail", id="Invalid URI"),
    pytest.param({"type": "Follow", "object": "https://server.test/"}, "pass", id="Valid URI"),
    pytest.param(
        {"type": "Accept", "object": "https://server.test/"},
        "pass",
        id="Valid URI - Accept",
    ),
    pytest.param(
        {"type": "Reject", "object": "https://server.test/"},
        "pass",
        id="Valid URI - Reject",
    ),
    pytest.param(
        {"type": "Follow", "object": "https://server.test/", "EXTRA": "extra"},
        "pass",
        id="Extra properties",
    ),
    pytest.param(
        {
            "type": "Follow",
            "object": {
                "id": "https://server.test/",
                "type": ["Note", "Custom"],
            },
        },
        "pass",
        id="Allows multiple types for 'object'",
    ),
    pytest.param(
        {
            "type": "Follow",
            "object": "https://server.test/",
            "acter": "Mel Brooks",
        },
        "pass",
        # AS2 is very forgiving of extra properties
        id="Mispelled 'actor'... passes as extra property",
    ),
]


@pytest.mark.parametrize(["activity", "executed_outcome"], VALIDATION_TEST_CASES)
def test_activity_validation(activity: dict, executed_outcome: str, validator):
    try:
        validator.validate(activity)
        assert executed_outcome == "pass", lambda: f"Unexpected pass: {activity}"
    except ValidationError as ex:
        assert executed_outcome == "fail", ex.message
