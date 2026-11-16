"""Shared JSON Schema validation for registered tool arguments."""

from copy import deepcopy
from typing import Any, Mapping

from jsonschema import Draft202012Validator


def checked_schema(schema: Mapping[str, Any] | None) -> dict[str, Any]:
    # Legacy registrations remain object-valued; explicit schemas are required
    # for precise LLM tools. Never infer argument names from Python handlers.
    result = deepcopy(dict(schema)) if schema is not None else {"type": "object"}
    Draft202012Validator.check_schema(result)
    return result


def validate_arguments(args: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(dict(args)), key=lambda e: str(e.path))
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.path) or "arguments"
        raise ValueError(f"{path}: {error.message}")
