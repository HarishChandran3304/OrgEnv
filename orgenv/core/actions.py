"""Universal action representation and handler types."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .models import WorldState
from .tool_schema import checked_schema, validate_arguments


@dataclass(frozen=True)
class Action:
    actor_id: str
    operation: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", deepcopy(dict(self.payload)))

    def validate(self) -> None:
        if not isinstance(self.actor_id, str) or not self.actor_id.strip():
            raise ValueError("action actor_id must be a non-empty string")
        if not isinstance(self.operation, str) or not self.operation.strip():
            raise ValueError("action operation must be a non-empty string")
        if not isinstance(self.payload, Mapping):
            raise ValueError("action payload must be a mapping")

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "operation": self.operation,
            "payload": deepcopy(dict(self.payload)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Action:
        return cls(
            actor_id=value.get("actor_id", ""),
            operation=value.get("operation", ""),
            payload=value.get("payload", {}),
        )


class ActionExecutionError(ValueError):
    """A handler can raise this to reject an otherwise registered action."""


class Handler(Protocol):
    def __call__(self, state: WorldState, action: Action) -> Any: ...


@dataclass(frozen=True)
class OperationDefinition:
    name: str
    handler: Handler
    description: str = ""
    input_schema: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", checked_schema(self.input_schema))

    def validate(self, action: Action) -> None:
        validate_arguments(action.payload, self.input_schema)

    def tool_description(self) -> dict[str, Any]:
        return {"name": self.name, "kind": "action", "read_only": False,
                "description": self.description, "input_schema": deepcopy(self.input_schema)}
