"""Task-defined, actor-bound read-only information capabilities."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .actions import Action
from .models import WorldState
from .policies import AuthorizationDecision
from .tool_schema import checked_schema, validate_arguments


class CapabilityValidationError(ValueError):
    """Raised when capability arguments do not satisfy the task schema."""


class CapabilityExecutionError(RuntimeError):
    """Raised when a capability cannot produce a result."""


CapabilityHandler = Callable[[WorldState, str, Mapping[str, Any]], Any]
CapabilityValidator = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class CapabilityPolicy(Protocol):
    def authorize(self, state: WorldState, actor_id: str, args: Mapping[str, Any]) -> AuthorizationDecision: ...


@dataclass(frozen=True)
class CapabilityDefinition:
    name: str
    handler: CapabilityHandler
    validator: CapabilityValidator | None = None
    policy: CapabilityPolicy | Callable[..., Any] | None = None
    description: str = ""
    input_schema: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", checked_schema(self.input_schema))

    def tool_description(self) -> dict[str, Any]:
        return {"name": self.name, "kind": "capability", "read_only": True,
                "description": self.description, "input_schema": deepcopy(self.input_schema)}

    def validate_args(self, args: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(args, Mapping):
            raise CapabilityValidationError("capability arguments must be a mapping")
        candidate = deepcopy(dict(args))
        try:
            validate_arguments(candidate, self.input_schema)
        except ValueError as exc:
            raise CapabilityValidationError(str(exc)) from exc
        if self.validator is not None:
            try:
                validated = self.validator(candidate)
            except CapabilityValidationError:
                raise
            except Exception as exc:
                raise CapabilityValidationError(str(exc) or "invalid capability arguments") from exc
            if not isinstance(validated, Mapping):
                raise CapabilityValidationError("capability validator must return a mapping")
            candidate = deepcopy(dict(validated))
        return candidate

    def authorize(self, state: WorldState, actor_id: str, args: Mapping[str, Any]) -> AuthorizationDecision:
        if self.policy is None:
            return AuthorizationDecision.allow()
        action = Action(actor_id, self.name, args)
        policy_authorize = getattr(self.policy, "authorize", None)
        if policy_authorize is not None:
            try:
                decision = policy_authorize(state, action)
            except TypeError:
                decision = policy_authorize(state, actor_id, args)
        else:
            decision = self.policy(state, actor_id, args)
        if isinstance(decision, AuthorizationDecision):
            return decision
        if isinstance(decision, bool):
            return AuthorizationDecision.allow() if decision else AuthorizationDecision.deny("capability access denied")
        raise CapabilityExecutionError("capability policy must return AuthorizationDecision or bool")
