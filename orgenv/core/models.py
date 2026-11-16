"""Authoritative, domain-independent world state models."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping


def _copy_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return deepcopy(dict(value))


@dataclass
class Actor:
    """A participant in an organizational world."""

    id: str
    role: str
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("actor id must be a non-empty string")
        if not isinstance(self.role, str) or not self.role.strip():
            raise ValueError("actor role must be a non-empty string")
        self.attributes = _copy_mapping(self.attributes)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "role": self.role, "attributes": deepcopy(self.attributes)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Actor:
        return cls(value["id"], value["role"], value.get("attributes", {}))


@dataclass
class Resource:
    """A mutable resource owned by an actor or the organization."""

    id: str
    type: str
    owner_id: str | None = None
    state: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("resource id must be a non-empty string")
        if not isinstance(self.type, str) or not self.type.strip():
            raise ValueError("resource type must be a non-empty string")
        self.state = _copy_mapping(self.state)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "owner_id": self.owner_id,
            "state": deepcopy(self.state),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Resource:
        return cls(value["id"], value["type"], value.get("owner_id"), value.get("state", {}))


@dataclass
class WorldState:
    """Complete authoritative state for one task instance.

    Callers should use :meth:`clone` or :meth:`to_dict`; the engine never returns
    this live object, which keeps external mutations from bypassing transitions.
    """

    actors: dict[str, Actor] = field(default_factory=dict)
    resources: dict[str, Resource] = field(default_factory=dict)
    task_data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.actors = {actor_id: deepcopy(actor) for actor_id, actor in self.actors.items()}
        self.resources = {resource_id: deepcopy(resource) for resource_id, resource in self.resources.items()}
        self.task_data = deepcopy(self.task_data)
        self._validate_indexes()

    def _validate_indexes(self) -> None:
        for key, actor in self.actors.items():
            if key != actor.id:
                raise ValueError(f"actor index {key!r} does not match actor id {actor.id!r}")
        for key, resource in self.resources.items():
            if key != resource.id:
                raise ValueError(f"resource index {key!r} does not match resource id {resource.id!r}")
        unknown_owners = {
            resource.owner_id
            for resource in self.resources.values()
            if resource.owner_id is not None and resource.owner_id not in self.actors
        }
        if unknown_owners:
            raise ValueError(f"resource owner(s) do not exist: {sorted(unknown_owners)}")

    def get_actor(self, actor_id: str) -> Actor:
        try:
            return self.actors[actor_id]
        except KeyError as exc:
            raise KeyError(f"unknown actor: {actor_id}") from exc

    def get_resource(self, resource_id: str) -> Resource:
        try:
            return self.resources[resource_id]
        except KeyError as exc:
            raise KeyError(f"unknown resource: {resource_id}") from exc

    def clone(self) -> WorldState:
        return deepcopy(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "actors": {key: actor.to_dict() for key, actor in sorted(self.actors.items())},
            "resources": {key: resource.to_dict() for key, resource in sorted(self.resources.items())},
            "task_data": deepcopy(self.task_data),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorldState:
        actors = {key: Actor.from_dict(actor) for key, actor in value.get("actors", {}).items()}
        resources = {key: Resource.from_dict(resource) for key, resource in value.get("resources", {}).items()}
        return cls(actors=actors, resources=resources, task_data=value.get("task_data", {}))
