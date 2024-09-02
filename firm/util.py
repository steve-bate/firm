import importlib.metadata
import logging
import os
import tomllib
from typing import Any, Mapping

from firm.interfaces import JSON, JSONObject

log = logging.getLogger(__name__)


def has_value(resource: JSONObject, key: str, value: str) -> bool:
    resource_value = resource.get(key)
    return (
        isinstance(resource_value, str)
        and resource_value == value
        or isinstance(resource_value, list)
        and value in resource_value
    )


def resource_get(resource: JSONObject, *keys: str, default: Any | None = None) -> JSON:
    resource_value: JSON | None = resource
    for key in keys:
        if resource_value is None or not isinstance(resource_value, Mapping):
            return default
        resource_value = resource_value.get(key)
    return resource_value


def resource_id(resource: JSON | str) -> str:
    if isinstance(resource, str):
        return resource
    if isinstance(resource, Mapping):
        return str(resource["id"])
    raise ValueError(f"Get get ID from resource: {resource}")


def _get_version_from_pyproject() -> str | None:
    try:
        cwd = os.getcwd()
        while True:
            candidate_path = os.path.join(cwd, "pyproject.toml")
            if os.path.exists(candidate_path):
                with open(candidate_path, "rb") as f:
                    pyproject_data = tomllib.load(f)
                    return (
                        pyproject_data.get("tool", {})
                        .get("poetry", {})
                        .get("version", "?")
                    )
            if cwd == "/":
                return None
            cwd = os.path.dirname(cwd)
    except FileNotFoundError:
        return None


def _package_version(package_name) -> str | None:
    try:
        return importlib.metadata.version(package_name)
    except importlib.metadata.PackageNotFoundError:
        return None


def get_version(package_name, default_version="0.0.0-dev") -> str:
    return (
        _package_version(package_name)
        or _get_version_from_pyproject()
        or default_version
    )
