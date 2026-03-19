import importlib.metadata
import logging
import os
import tomllib
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from firm.core.interfaces import JSON, APActor, JSONObject

log = logging.getLogger(__name__)

AP_PUBLIC_URIS = [
    "https://www.w3.org/ns/activitystreams#Public",
    "as:Public",
    "Public",
]

AS2_CONTENT_TYPES = [
    "application/activity+json",
    'application/ld+json; profile="https://www.w3.org/ns/activitystreams"',
]

AS2_ACTOR_TYPES = {
    "Application",
    "Group",
    "Organization",
    "Person",
    "Service",
}

RECIPIENT_FIELDS = {
    "to",
    "bto",
    "cc",
    "bcc",
    "audience",
}

ACTIVITIES_REQUIRING_OBJECT = {
    "Create",
    "Update",
    "Delete",
    "Follow",
    "Add",
    "Remove",
    "Like",
    "Block",
    "Undo",
}

ACTIVITIES_REQUIRING_TARGET = {
    "Add",
    "Remove",
}


def is_activity(resource: JSONObject) -> bool:
    # duck typing
    return "actor" in resource and "object" in resource


def is_public(resource: JSONObject | str) -> bool:
    if isinstance(resource, str):
        return resource in AP_PUBLIC_URIS
    for key in RECIPIENT_FIELDS:
        if key in resource:
            for uri in AP_PUBLIC_URIS:
                if has_value(resource, key, uri):
                    return True
    return False


def is_audience(uri: str, resource: JSONObject) -> bool:
    for key in RECIPIENT_FIELDS:
        if has_value(resource, key, uri):
            return True
    return False


def is_accessible(subject_uri: str | None, resource: JSONObject) -> bool:
    return is_public(resource) or (
        subject_uri is not None
        and (
            has_value(resource, "attributedTo", subject_uri)
            or has_value(resource, "actor", subject_uri)
            or is_audience(subject_uri, resource)
        )
    )


def get_types(resource: JSONObject) -> list[str]:
    types: Any = resource.get("type", [])
    if isinstance(types, str):
        types = [types]
    return types


def is_type(resource, resource_type: str) -> bool:
    return has_value(resource, "type", resource_type)


def is_type_any(resource, resource_types: Iterable[str]) -> bool:
    return any(is_type(resource, t) for t in resource_types)


def is_actor_object(resource: JSONObject) -> bool:
    return any(t in AS2_ACTOR_TYPES for t in get_types(resource))


def is_actor_collection(actor: APActor, resource_uri: str) -> bool:
    for key in ["followers", "following", "liked", "shares"]:
        if actor.get(key) == resource_uri:
            return True
    return False


def is_recipient(resource: JSONObject, uri: str) -> bool:
    for key in RECIPIENT_FIELDS:
        value = resource.get(key)
        if value:
            if isinstance(value, str):
                if value == uri:
                    return True
            elif isinstance(value, list):
                if any(v == uri for v in value):
                    return True
    return False


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
        if "id" in resource:
            return str(resource["id"])
    raise ValueError(f"Get get ID from resource: {resource}")


# TODO pending restructuring
# Slight difference in behavior from resource_id
def get_id(resource: JSON | str) -> str | None:
    if isinstance(resource, str):
        return resource
    if isinstance(resource, Mapping) and "id" in resource:
        return str(resource["id"])
    return None


def _get_version_from_pyproject() -> str | None:
    try:
        cwd = os.getcwd()
        while True:
            candidate_path = os.path.join(cwd, "pyproject.toml")
            if os.path.exists(candidate_path):
                with open(candidate_path, "rb") as f:
                    pyproject_data = tomllib.load(f)
                    return pyproject_data.get("tool", {}).get("poetry", {}).get("version", "?")
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
    return _package_version(package_name) or _get_version_from_pyproject() or default_version


def get_prefix_uri(uri: str, scheme_override: str | None = None) -> str:
    """Get the instance "prefix" for the uri"""
    url = urlparse(uri)
    return f"{scheme_override or url.scheme}://{url.netloc}"


def is_collection(obj: JSONObject) -> bool:
    for type_ in ["Collection", "CollectionPage", "OrderedCollection", "OrderedCollectionPage"]:
        if has_value(obj, "type", type_):
            return True
    return False


def is_ordered_collection(obj: JSONObject) -> bool:
    for type_ in ["OrderedCollection", "OrderedCollectionPage"]:
        if has_value(obj, "type", type_):
            return True
    return False


def get_collection_items_key(collection: JSONObject) -> str:
    if is_ordered_collection(collection):
        return "orderedItems"
    else:
        return "items"


def get_collection_items(collection: JSONObject) -> list[JSON]:
    items_key = get_collection_items_key(collection)
    items = collection.get(items_key, [])
    if isinstance(items, list):
        return items
    else:
        return []


def set_collection_items(collection: JSONObject, items: list[JSON]) -> None:
    items_key = get_collection_items_key(collection)
    collection[items_key] = items


def get_list(obj: JSONObject, key: str) -> list[JSON]:
    value = obj.get(key)
    if value is None:
        return []
    elif isinstance(value, list):
        return value
    else:
        return [value]
