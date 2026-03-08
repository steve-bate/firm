import importlib.resources as pkg_resources
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import referencing
import referencing.retrieval
from jsonschema import Draft202012Validator
from jsonschema.protocols import Validator


def create_schema_retriever(
    *,
    schema_dirs: list[Path] | None = None,
    package_names: Iterable[str] | None = None,
):
    @referencing.retrieval.to_cached_resource()
    def _retriever(uri) -> str:
        if schema_dirs:
            for directory in schema_dirs:
                url = urlparse(uri)
                filepath = directory / f"{url.path}-schema.json"
                if filepath.exists() and filepath.is_file():
                    with open(filepath) as fp:
                        return fp.read()
        if package_names:
            for package_name in package_names:
                try:
                    url = urlparse(uri)
                    url_path = Path(url.path)
                    schema_package_name = package_name
                    if len(url_path.parts) > 1:
                        schema_package_name += "." + str(url_path.parent).replace("/", ".")
                    resource_path = f"{url_path.name}-schema.json"
                    return (
                        pkg_resources.files(schema_package_name).joinpath(resource_path).read_text()
                    )
                except FileNotFoundError:
                    pass
        raise FileNotFoundError(f"Schema not found: {uri}")

    return _retriever


def create_validator(
    *,
    root_schema: str,
    schema_dirs: list[Path] | None = None,
    package_names: Iterable[str] | None = None,
    registry_callback: Callable[[referencing.Registry], referencing.Registry] = lambda x: x,
) -> Validator:
    schema_retriever = create_schema_retriever(
        schema_dirs=schema_dirs,
        package_names=package_names,
    )
    return Draft202012Validator(
        schema_retriever(root_schema).contents,
        registry=registry_callback(referencing.Registry(retrieve=schema_retriever)),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def validate_activity(activity: dict[str, Any], validator: Validator) -> dict[str, Any]:
    """Validates an activity and returns it, if valid."""
    validator.validate(activity)
    return activity
