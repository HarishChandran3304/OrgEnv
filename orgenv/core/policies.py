"""Small composable authorization policies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .actions import Action
from .models import WorldState


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason: str = ""

    @classmethod
    def allow(cls) -> AuthorizationDecision:
        return cls(True)

    @classmethod
    def deny(cls, reason: str) -> AuthorizationDecision:
        return cls(False, reason)


class AuthorizationPolicy(Protocol):
    def authorize(self, state: WorldState, action: Action) -> AuthorizationDecision: ...


@dataclass(frozen=True)
class RolePolicy:
    """Allow an operation only for actors whose role is in ``allowed_roles``."""

    operation: str
    allowed_roles: frozenset[str]

    def __init__(self, operation: str, allowed_roles: set[str] | frozenset[str]) -> None:
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "allowed_roles", frozenset(allowed_roles))

    def authorize(self, state: WorldState, action: Action) -> AuthorizationDecision:
        if action.operation != self.operation:
            return AuthorizationDecision.allow()
        actor = state.actors.get(action.actor_id)
        if actor is None:
            return AuthorizationDecision.deny("actor does not exist")
        if actor.role not in self.allowed_roles:
            roles = ", ".join(sorted(self.allowed_roles))
            return AuthorizationDecision.deny(f"actor role {actor.role!r} is not authorized; requires {roles}")
        return AuthorizationDecision.allow()


@dataclass(frozen=True)
class ApprovalRequiredPolicy:
    """Require a resource's state to contain a truthy approval marker."""

    operation: str
    resource_payload_key: str = "resource_id"
    approval_state_key: str = "approved"

    def authorize(self, state: WorldState, action: Action) -> AuthorizationDecision:
        if action.operation != self.operation:
            return AuthorizationDecision.allow()
        resource_id = action.payload.get(self.resource_payload_key)
        resource = state.resources.get(resource_id)
        if resource is None:
            return AuthorizationDecision.deny("referenced resource does not exist")
        if not resource.state.get(self.approval_state_key, False):
            return AuthorizationDecision.deny("resource has not been approved")
        return AuthorizationDecision.allow()
