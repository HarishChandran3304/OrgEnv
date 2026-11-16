"""Minimal task contract used by the core engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from orgenv.core.actions import Action, Handler, OperationDefinition
from orgenv.core.actors import ActorSpec
from orgenv.core.capabilities import CapabilityDefinition
from orgenv.core.models import WorldState
from orgenv.core.policies import AuthorizationPolicy

ObservationFn = Callable[[WorldState], Any]
SuccessFn = Callable[[WorldState], bool]
RewardFn = Callable[[WorldState, Action, bool], float]
EventHandler = Callable[..., Any]
VisibilityFn = Callable[[Mapping[str, Any]], Mapping[str, Any]]


@dataclass
class TaskDefinition:
    initial_state: WorldState
    observation_fn: ObservationFn = lambda state: state.to_dict()
    success_condition: SuccessFn = lambda state: False
    operations: dict[str, Handler | OperationDefinition] = field(default_factory=dict)
    policies: list[AuthorizationPolicy] = field(default_factory=list)
    reward_fn: RewardFn | None = None
    event_handlers: dict[str, EventHandler] = field(default_factory=dict)
    actor_specs: dict[str, ActorSpec] = field(default_factory=dict)
    visibility_fn: VisibilityFn | None = None
    capabilities: dict[str, CapabilityDefinition] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.initial_state = self.initial_state.clone()
        self.operations = dict(self.operations)
        self.policies = list(self.policies)
        self.event_handlers = dict(self.event_handlers)
        self.actor_specs = {actor_id: spec for actor_id, spec in self.actor_specs.items()}
        self.capabilities = dict(self.capabilities)

    def register_operation(self, name: str, handler: Handler, *, description: str = "", input_schema: Mapping[str, Any] | None = None) -> TaskDefinition:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("operation name must be a non-empty string")
        if name in self.operations:
            raise ValueError(f"operation already registered: {name}")
        self.operations[name] = OperationDefinition(name, handler, description, input_schema)
        return self

    def add_policy(self, policy: AuthorizationPolicy) -> TaskDefinition:
        self.policies.append(policy)
        return self

    def clone_initial_state(self) -> WorldState:
        return self.initial_state.clone()

    def register_event_handler(self, event_type: str, handler: EventHandler) -> TaskDefinition:
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event type must be a non-empty string")
        if event_type in self.event_handlers:
            raise ValueError(f"event handler already registered: {event_type}")
        self.event_handlers[event_type] = handler
        return self

    def register_actor(self, spec: ActorSpec) -> TaskDefinition:
        if spec.actor_id in self.actor_specs:
            raise ValueError(f"actor already registered: {spec.actor_id}")
        self.actor_specs[spec.actor_id] = spec
        return self

    def register_capability(
        self,
        capability: CapabilityDefinition | str | None = None,
        handler: Any = None,
        policy: Any = None,
        validator: Any = None,
        *,
        name: str | None = None,
        description: str = "",
        input_schema: Mapping[str, Any] | None = None,
    ) -> TaskDefinition:
        if capability is None:
            capability = name
        if isinstance(capability, str):
            if handler is None:
                raise ValueError("capability handler is required")
            capability = CapabilityDefinition(capability, handler, validator, policy, description, input_schema)
        if not isinstance(capability, CapabilityDefinition):
            raise TypeError("capability must be a CapabilityDefinition or name")
        if not capability.name.strip():
            raise ValueError("capability name must be a non-empty string")
        if capability.name in self.capabilities:
            raise ValueError(f"capability already registered: {capability.name}")
        self.capabilities[capability.name] = capability
        return self

