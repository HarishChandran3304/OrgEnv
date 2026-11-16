"""Policy-driven actor runtime primitives."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from .actions import Action


@dataclass(frozen=True)
class ActorProfile:
    """Reusable, intentionally small behavioral context for an actor policy."""

    role: str
    responsibilities: tuple[str, ...] = ()
    behavioral_instructions: str = ""
    priorities: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "responsibilities": list(self.responsibilities),
            "behavioral_instructions": self.behavioral_instructions,
            "priorities": list(self.priorities),
        }


@dataclass(frozen=True)
class MessageRequest:
    """A policy's sender-bound request to send plain-text content."""

    recipient_id: str
    content: str
    delay: float = 0.0


@dataclass(frozen=True)
class CapabilityRequest:
    capability: str
    args: Mapping[str, Any] = field(default_factory=dict)


class ActorHandle:
    """Identity-bound facade for APIs given to one actor or focal controller."""

    def __init__(self, runtime: Any, actor_id: str) -> None:
        self._runtime = runtime
        self.actor_id = actor_id

    def observe(self) -> dict[str, Any]:
        return self._runtime.actor_observation(self.actor_id)

    def query(self, capability: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._runtime.query(self.actor_id, capability, args)

    def send_message(self, recipient_id: str, content: str, delay: float = 0.0) -> str:
        return self._runtime.send_message(self.actor_id, recipient_id, content, delay)


@dataclass(frozen=True)
class ActorDecision:
    action: Action | None = None
    messages: tuple[MessageRequest, ...] = ()
    queries: tuple[CapabilityRequest, ...] = ()
    validation_errors: tuple[str, ...] = ()


class ActorPolicy(Protocol):
    def decide(self, observation: Mapping[str, Any]) -> ActorDecision | Action | None: ...


class DeterministicPolicy:
    """Small reference policy adapter for deterministic task-defined rules."""

    def __init__(self, decision_fn: Callable[[Mapping[str, Any]], ActorDecision | Action | None]) -> None:
        self._decision_fn = decision_fn

    def decide(self, observation: Mapping[str, Any]) -> ActorDecision | Action | None:
        return self._decision_fn(deepcopy(dict(observation)))


class NoOpPolicy:
    def decide(self, observation: Mapping[str, Any]) -> None:
        return None


@dataclass
class ActorSpec:
    actor_id: str
    policy: ActorPolicy | None = None
    private_knowledge: dict[str, Any] = field(default_factory=dict)
    visible_resource_ids: frozenset[str] = frozenset()
    profile: ActorProfile | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.actor_id, str) or not self.actor_id.strip():
            raise ValueError("actor spec id must be a non-empty string")
        self.private_knowledge = deepcopy(self.private_knowledge)
        self.visible_resource_ids = frozenset(self.visible_resource_ids)


@dataclass
class Message:
    message_id: str
    sender_id: str
    recipient_id: str
    content: str
    sent_at: float
    delivery_at: float
    status: str = "scheduled"
    action_receipt: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender_id": self.sender_id,
            "recipient_id": self.recipient_id,
            "content": self.content,
            "sent_at": self.sent_at,
            "delivery_at": self.delivery_at,
            "status": self.status,
            "action_receipt": deepcopy(self.action_receipt),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Message":
        return cls(
            message_id=value["message_id"],
            sender_id=value["sender_id"],
            recipient_id=value["recipient_id"],
            content=value["content"],
            sent_at=float(value["sent_at"]),
            delivery_at=float(value["delivery_at"]),
            status=value.get("status", "scheduled"),
            action_receipt=deepcopy(value.get("action_receipt")),
        )


@dataclass
class ActorRuntime:
    spec: ActorSpec
    inbox: list[Message] = field(default_factory=list)
    outbox: list[Message] = field(default_factory=list)
    action_results: list[dict[str, Any]] = field(default_factory=list)
    capability_results: list[dict[str, Any]] = field(default_factory=list)
    decision_records: list[dict[str, Any]] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {
            "actor_id": self.spec.actor_id,
            "private_knowledge": deepcopy(self.spec.private_knowledge),
            "inbox": [message.to_dict() for message in self.inbox],
            "outbox": [message.to_dict() for message in self.outbox],
            "action_results": deepcopy(self.action_results),
            "capability_results": deepcopy(self.capability_results),
            "decision_records": deepcopy(self.decision_records),
        }

    def restore_runtime(self, value: Mapping[str, Any]) -> None:
        self.inbox = [Message.from_dict(message) for message in value.get("inbox", [])]
        self.outbox = [Message.from_dict(message) for message in value.get("outbox", [])]
        self.action_results = deepcopy(value.get("action_results", []))
        self.capability_results = deepcopy(value.get("capability_results", []))
        self.decision_records = deepcopy(value.get("decision_records", []))
