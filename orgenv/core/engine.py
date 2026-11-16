"""Deterministic transition engine with simulated time and scheduling."""

from __future__ import annotations

import inspect
import math
from copy import deepcopy
from typing import Any, Mapping, Sequence

from .actions import Action, ActionExecutionError, Handler, OperationDefinition
from .actors import (
    ActorDecision,
    ActorHandle,
    ActorRuntime,
    ActorSpec,
    CapabilityRequest,
    Message,
    MessageRequest,
)
from .capabilities import CapabilityDefinition, CapabilityValidationError
from .clock import SimulationClock
from .events import EventLog, StateChange, TransitionEvent
from .models import WorldState
from .policies import AuthorizationPolicy
from .randomness import RandomManager
from .runtime import ExecutionContext
from .scheduler import EventPrecondition, EventScheduler, ScheduledEvent
from orgenv.task import TaskDefinition

MESSAGE_EVENT_TYPE = "__orgenv_message_delivery__"


_MISSING = object()


class EventProcessingLimitError(RuntimeError):
    """Raised when due events exceed the configured per-advance bound."""


class ReplayError(ValueError):
    """Raised when a recorded run cannot be reproduced consistently."""


class ActorProcessingLimitError(RuntimeError):
    """Raised when autonomous actor decisions exceed their configured bound."""


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key in sorted(value, key=str):
            child = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten(value[key], child))
        return result
    return {prefix: deepcopy(value)}


def _state_changes(before: dict[str, Any], after: dict[str, Any]) -> tuple[StateChange, ...]:
    before_flat, after_flat = _flatten(before), _flatten(after)
    paths = sorted(set(before_flat) | set(after_flat))
    return tuple(
        StateChange(
            path,
            None if before_flat.get(path, _MISSING) is _MISSING else before_flat[path],
            None if after_flat.get(path, _MISSING) is _MISSING else after_flat[path],
        )
        for path in paths
        if before_flat.get(path, _MISSING) != after_flat.get(path, _MISSING)
    )


def _invoke(handler: Any, state: WorldState, item: Any, context: ExecutionContext) -> Any:
    """Invoke old two-argument handlers and new context-aware handlers."""
    try:
        signature = inspect.signature(handler)
        try:
            signature.bind(state, item, context)
        except TypeError:
            return handler(state, item)
    except (TypeError, ValueError):
        return handler(state, item)
    return handler(state, item, context)


class OrgEnv:
    """An RL-shaped deterministic environment for one task definition."""

    def __init__(
        self,
        task: TaskDefinition,
        max_events_per_advance: int = 1000,
        max_actor_actions_per_advance: int = 100,
        max_actor_decisions_per_advance: int | None = None,
        max_actor_tools_per_advance: int = 100,
    ) -> None:
        if max_events_per_advance <= 0:
            raise ValueError("max_events_per_advance must be positive")
        if max_actor_actions_per_advance <= 0:
            raise ValueError("max_actor_actions_per_advance must be positive")
        if max_actor_tools_per_advance <= 0:
            raise ValueError("max_actor_tools_per_advance must be positive")
        self.max_actor_tools_per_advance = max_actor_tools_per_advance
        self.task = task
        self.max_events_per_advance = max_events_per_advance
        self.max_actor_actions_per_advance = max_actor_actions_per_advance
        self.max_actor_decisions_per_advance = (
            max_actor_actions_per_advance if max_actor_decisions_per_advance is None else max_actor_decisions_per_advance
        )
        if self.max_actor_decisions_per_advance <= 0:
            raise ValueError("max_actor_decisions_per_advance must be positive")
        self._state = task.clone_initial_state()
        self._events = EventLog()
        self._clock = SimulationClock()
        self._scheduler = EventScheduler()
        self._random = RandomManager()
        self._actors: dict[str, ActorRuntime] = {}
        self._messages: dict[str, Message] = {}
        self._next_message_id = 1
        self._autonomous_actions = 0
        self._actor_decisions = 0
        self._actor_queries = 0
        self._actor_tools = 0
        self._seed: int | None = None
        self._terminated = False
        self._initialize_actor_runtime()

    def reset(self, seed: int | None = None) -> Any:
        self._seed = seed
        self._state = self.task.clone_initial_state()
        self._events.clear()
        self._clock.reset()
        self._scheduler.clear()
        self._random.reset(seed)
        self._initialize_actor_runtime()
        self._autonomous_actions = 0
        self._actor_decisions = 0
        self._actor_queries = 0
        self._actor_tools = 0
        self._terminated = False
        return self._observation()

    @property
    def current_time(self) -> float:
        return self._clock.now

    @property
    def time(self) -> float:
        """Alias for :attr:`current_time` for concise task code."""
        return self.current_time

    @property
    def pending_events(self) -> tuple[dict[str, Any], ...]:
        return self._scheduler.pending()

    @property
    def actor_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._actors))

    def actor(self, actor_id: str) -> ActorHandle:
        """Return an identity-bound facade; it cannot select another sender."""
        self._get_actor(actor_id)
        return ActorHandle(self, actor_id)

    @property
    def messages(self) -> tuple[dict[str, Any], ...]:
        """Return detached message records ordered by message identifier."""
        return tuple(deepcopy(self._messages[key].to_dict()) for key in sorted(self._messages))

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return self._events.as_dicts()

    @property
    def seed(self) -> int | None:
        return self._seed

    def get_state(self) -> dict[str, Any]:
        """Return a detached, serialized authoritative-state snapshot."""
        return self._state.to_dict()

    def actor_observation(self, actor_id: str) -> dict[str, Any]:
        """Return a detached, visibility-filtered observation for one actor."""
        runtime = self._get_actor(actor_id)
        decision_history = [
            {
                key: deepcopy(record[key])
                for key in ("kind", "model", "temperature", "validation", "decision")
                if key in record
            }
            for record in runtime.decision_records
        ]
        public: dict[str, Any] = {
            "actor_id": actor_id,
            "public": deepcopy(self._state.task_data.get("public", {})),
            "actors": {
                key: {"id": actor.id, "role": actor.role}
                for key, actor in sorted(self._state.actors.items())
            },
            "resources": {},
            "private_knowledge": deepcopy(runtime.spec.private_knowledge),
            "inbox": [message.to_dict() for message in runtime.inbox],
            "own_action_results": deepcopy(runtime.action_results),
            "capability_results": deepcopy(runtime.capability_results),
            "decision_history": decision_history,
            "tools": self._actor_tools_catalog(runtime),
        }
        for resource_id, resource in sorted(self._state.resources.items()):
            resource_view = {"id": resource.id, "type": resource.type, "owner_id": resource.owner_id}
            if resource_id in runtime.spec.visible_resource_ids:
                resource_view["state"] = deepcopy(resource.state)
            public["resources"][resource_id] = resource_view
        if self.task.visibility_fn is not None:
            public = deepcopy(dict(self.task.visibility_fn(public)))
        return deepcopy(public)

    def get_actor_runtime(self, actor_id: str) -> dict[str, Any]:
        """Return a detached runtime snapshot for debugging, not for policies."""
        return self._get_actor(actor_id).snapshot()

    def _actor_tools_catalog(self, runtime: ActorRuntime) -> dict[str, Any]:
        policy = runtime.spec.policy
        actions = getattr(policy, "available_operations", ())
        capabilities = getattr(policy, "available_capabilities", ())
        return {
            "actions": {name: (op if isinstance(op, OperationDefinition) else
                                 OperationDefinition(name, op)).tool_description()
                        for name, op in self.task.operations.items() if name in actions},
            "capabilities": {name: cap.tool_description() for name, cap in self.task.capabilities.items()
                             if name in capabilities},
        }

    def register_operation(self, name: str, handler: Handler, *, description: str = "",
                           input_schema: Mapping[str, Any] | None = None) -> None:
        self.task.register_operation(name, handler, description=description, input_schema=input_schema)

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
    ) -> None:
        """Register a task-owned read-only capability.

        Passing a ``CapabilityDefinition`` is the explicit form. The string
        form is a convenience matching ``env.register_capability(name=...)``.
        """
        if capability is None:
            capability = name
        if isinstance(capability, str):
            if handler is None:
                raise ValueError("capability handler is required")
            capability = CapabilityDefinition(capability, handler, validator, policy, description, input_schema)
        self.task.register_capability(capability)

    def register_event_handler(self, event_type: str, handler: Any) -> None:
        if event_type == MESSAGE_EVENT_TYPE:
            raise ValueError(f"{MESSAGE_EVENT_TYPE} is reserved for runtime message delivery")
        self.task.register_event_handler(event_type, handler)

    def add_policy(self, policy: AuthorizationPolicy) -> None:
        self.task.add_policy(policy)

    def query(self, actor_id: str, capability: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Invoke a capability as a specific actor and record the invocation."""
        runtime = self._get_actor(actor_id)
        before = self._state.to_dict()
        runtime_before = self._runtime_snapshot()
        raw_args = {} if args is None else args
        request = {"actor_id": actor_id, "capability": capability, "args": deepcopy(dict(raw_args)) if isinstance(raw_args, Mapping) else raw_args}
        result: dict[str, Any]
        outcome = "succeeded"
        reason = ""
        definition = self.task.capabilities.get(capability) if isinstance(capability, str) else None
        if definition is None:
            outcome, reason = "invalid", "unknown capability"
            result = {"ok": False, "error": {"code": "unknown_capability", "message": "unknown capability"}}
        else:
            try:
                validated_args = definition.validate_args(raw_args)
            except CapabilityValidationError as exc:
                outcome, reason = "invalid", str(exc) or "invalid capability arguments"
                result = {"ok": False, "error": {"code": "invalid_arguments", "message": reason}}
            else:
                decision = definition.authorize(self._state.clone(), actor_id, validated_args)
                if not decision.allowed:
                    outcome, reason = "unauthorized", "capability access denied"
                    result = {"ok": False, "error": {"code": "unauthorized", "message": reason}}
                else:
                    try:
                        value = definition.handler(self._state.clone(), actor_id, deepcopy(validated_args))
                        value = deepcopy(value)
                        if isinstance(value, Mapping):
                            structured = deepcopy(dict(value))
                        else:
                            structured = {"value": value}
                        result = {"ok": True, "result": structured}
                    except Exception:
                        outcome, reason = "failed", "capability execution failed"
                        result = {"ok": False, "error": {"code": "execution_failed", "message": reason}}
        result.update({"actor_id": actor_id, "capability": capability, "args": deepcopy(dict(raw_args)) if isinstance(raw_args, Mapping) else raw_args, "sim_time": self.current_time})
        runtime.capability_results.append(deepcopy(result))
        after = self._state.to_dict()
        event = self._make_event(
            kind="query",
            sim_time=self.current_time,
            action={"actor_id": actor_id, "operation": "query", "payload": request},
            outcome=outcome,
            reason=reason,
            before=before,
            after=after,
            runtime_before=runtime_before,
            runtime_after=self._runtime_snapshot(),
            query_result=result,
        )
        self._events.append(event)
        return deepcopy(result)

    def _initialize_actor_runtime(self) -> None:
        self._actors = {}
        self._messages = {}
        self._next_message_id = 1
        for actor_id in sorted(self._state.actors):
            spec = self.task.actor_specs.get(actor_id, ActorSpec(actor_id))
            self._actors[actor_id] = ActorRuntime(spec=spec)

    def _get_actor(self, actor_id: str) -> ActorRuntime:
        try:
            return self._actors[actor_id]
        except KeyError as exc:
            raise KeyError(f"unknown actor: {actor_id}") from exc

    def _runtime_snapshot(self) -> dict[str, Any]:
        return {actor_id: runtime.snapshot() for actor_id, runtime in sorted(self._actors.items())}

    def _restore_runtime(self, value: Mapping[str, Any]) -> None:
        self._messages = {}
        for actor_id, snapshot in value.items():
            if actor_id not in self._actors:
                raise ReplayError(f"unknown actor runtime in replay: {actor_id}")
            self._actors[actor_id].restore_runtime(snapshot)
            for message in self._actors[actor_id].outbox + self._actors[actor_id].inbox:
                self._messages[message.message_id] = message
        numeric_ids = [
            int(message_id.removeprefix("message-"))
            for message_id in self._messages
            if message_id.startswith("message-") and message_id.removeprefix("message-").isdigit()
        ]
        self._next_message_id = max(numeric_ids, default=0) + 1

    def send_message(
        self,
        sender_id: str,
        recipient_id: str,
        content: str,
        delay: float = 0.0,
    ) -> str:
        """Send conversational text; it is never proof of a state transition."""
        return self._send_message(sender_id, recipient_id, content, delay)

    def _send_message(self, sender_id: str, recipient_id: str, content: str, delay: float = 0.0,
                      action_receipt: dict[str, Any] | None = None) -> str:
        """Schedule a message from an existing actor with explicit identity checks."""
        runtime_before = self._runtime_snapshot()
        self._get_actor(sender_id)
        self._get_actor(recipient_id)
        if not isinstance(content, str):
            raise ValueError("message content must be text")
        if not content.strip():
            raise ValueError("message content must not be empty")
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 0:
            raise ValueError("message delay must be a non-negative finite number")
        message_id = f"message-{self._next_message_id}"
        self._next_message_id += 1
        message = Message(
            message_id=message_id,
            sender_id=sender_id,
            recipient_id=recipient_id,
            content=content,
            sent_at=self.current_time,
            delivery_at=self.current_time + float(delay),
            action_receipt=deepcopy(action_receipt),
        )
        self._messages[message_id] = message
        self._get_actor(sender_id).outbox.append(message)
        scheduled = self._schedule(
            delay,
            MESSAGE_EVENT_TYPE,
            {"message_id": message_id, "sender_id": sender_id, "recipient_id": recipient_id, "content": content},
            None,
        )
        snapshot = self._state.to_dict()
        message_event = self._make_event(
            kind="message",
            sim_time=self.current_time,
            action={},
            outcome="scheduled",
            reason="",
            before=snapshot,
            after=snapshot,
            event_id=scheduled.event_id,
            event_type=MESSAGE_EVENT_TYPE,
            event_payload=message.to_dict(),
            scheduled_events=(scheduled.to_dict(),),
            runtime_before=runtime_before,
            runtime_after=self._runtime_snapshot(),
        )
        self._events.append(message_event)
        return message_id

    def run_actor(self, actor_id: str) -> dict[str, Any] | None:
        """Run one bounded activation, including follow-up decisions after queries."""
        self._actor_decisions = 0
        self._actor_tools = 0
        self._actor_queries = 0
        return self._run_actor_once(actor_id, enforce_budget=False)

    def _normalize_decision(self, decision: ActorDecision | Action | None) -> ActorDecision:
        if decision is None:
            return ActorDecision()
        if isinstance(decision, Action):
            return ActorDecision(action=decision)
        if isinstance(decision, ActorDecision):
            return decision
        raise ValueError("actor policy must return ActorDecision, Action, or None")

    def _policy_decide(self, actor_id: str, runtime: ActorRuntime, observation: Mapping[str, Any]) -> ActorDecision:
        self._actor_decisions += 1
        if self._actor_decisions > self.max_actor_decisions_per_advance:
            raise ActorProcessingLimitError(
                f"more than {self.max_actor_decisions_per_advance} actor policy decisions in one advance"
            )
        policy = runtime.spec.policy
        assert policy is not None
        state_before = self._state.to_dict()
        runtime_before = self._runtime_snapshot()
        if hasattr(policy, "last_record"):
            policy.last_record = None
        try:
            decision = self._normalize_decision(policy.decide(deepcopy(observation)))
        except Exception as exc:
            record = getattr(policy, "last_record", None)
            if record is None:
                record = {
                    "kind": "policy_decision",
                    "actor_id": actor_id,
                    "observation": deepcopy(dict(observation)),
                    "validation": {"ok": False, "error": str(exc) or exc.__class__.__name__},
                }
            runtime.decision_records.append(deepcopy(record))
            self._record_policy_event(actor_id, state_before, runtime_before, record, "failed")
            raise
        record = getattr(policy, "last_record", None)
        if record is not None:
            runtime.decision_records.append(deepcopy(record))
            self._record_policy_event(actor_id, state_before, runtime_before, record,
                                      "invalid" if decision.validation_errors else "succeeded")
        return decision

    def _record_policy_event(
        self,
        actor_id: str,
        state_before: dict[str, Any],
        runtime_before: dict[str, Any],
        record: Mapping[str, Any],
        outcome: str,
    ) -> None:
        event = self._make_event(
            kind="policy",
            sim_time=self.current_time,
            action={"actor_id": actor_id, "operation": "policy_decide", "payload": {}},
            outcome=outcome,
            reason="" if outcome == "succeeded" else str(record.get("validation", {}).get("error", "policy failed")),
            before=state_before,
            after=state_before,
            runtime_before=runtime_before,
            runtime_after=self._runtime_snapshot(),
            query_result=deepcopy(dict(record)),
        )
        self._events.append(event)

    def _consume_actor_tool(self) -> None:
        if self._actor_tools >= self.max_actor_tools_per_advance:
            raise ActorProcessingLimitError("actor tool-call budget exhausted")
        self._actor_tools += 1

    def _actor_feedback(self, actor_id: str, runtime: ActorRuntime, outcome: str, reason: str) -> dict[str, Any]:
        before = self._runtime_snapshot()
        record = {"kind": "runtime_feedback", "actor_id": actor_id,
                  "validation": {"ok": False, "error": reason}, "sim_time": self.current_time}
        runtime.decision_records.append(record)
        self._record_policy_event(actor_id, self.get_state(), before, record, "skipped")
        return {"outcome": outcome, "reason": reason, "sim_time": self.current_time}

    def _apply_actor_decision(self, actor_id: str, runtime: ActorRuntime, decision: ActorDecision) -> dict[str, Any] | None:
        # In a composite decision all messages depend on its action. A failed
        # action suppresses them. Standalone messages remain ordinary dialogue.
        receipt = None
        result = None
        if decision.action is not None:
            action = decision.action
            if not isinstance(action, Action):
                raise ValueError("actor decision action must be an Action")
            if action.actor_id != actor_id:
                before = self._runtime_snapshot()
                result = {"outcome": "invalid", "reason": "actor policy attempted to use another actor identity",
                          "sim_time": self.current_time}
                runtime.action_results.append(deepcopy(result))
                self._record_policy_event(actor_id, self.get_state(), before,
                                          {"validation": {"error": result["reason"]}}, "invalid")
                return result
            self._consume_actor_tool()
            _, reward, terminated, truncated, info = self.step(action, _actor_runtime_target=runtime)
            result = {"action": action.to_dict(), "outcome": info["outcome"], "reason": info["reason"],
                      "reward": reward, "terminated": terminated, "truncated": truncated,
                      "sim_time": self.current_time}
            if info["outcome"] != "succeeded":
                if decision.messages:
                    self._actor_feedback(actor_id, runtime, "messages_suppressed",
                                         "Action failed; dependent messages were not sent. See own_action_results.")
                return result
            receipt = {"sequence": info["event"]["sequence"], "actor_id": actor_id,
                       "operation": action.operation, "outcome": "succeeded"}
        for request in decision.messages:
            if not isinstance(request, MessageRequest):
                raise ValueError("actor decision messages must be MessageRequest values")
            self._consume_actor_tool()
            self._send_message(actor_id, request.recipient_id, request.content, request.delay, receipt)
        return result

    def _run_actor_once(self, actor_id: str, enforce_budget: bool) -> dict[str, Any] | None:
        runtime = self._get_actor(actor_id)
        if runtime.spec.policy is None:
            return None
        if enforce_budget:
            self._autonomous_actions += 1
            if self._autonomous_actions > self.max_actor_actions_per_advance:
                raise ActorProcessingLimitError(
                    f"more than {self.max_actor_actions_per_advance} autonomous actor decisions in one advance"
                )
        while True:
            try:
                decision = self._policy_decide(actor_id, runtime, self.actor_observation(actor_id))
                if decision.queries:
                    # Read phase: never execute an accompanying action/message
                    # from before the results. The next decision must resubmit it.
                    if decision.action is not None or decision.messages:
                        self._actor_feedback(actor_id, runtime, "deferred",
                                             "Query phase: action/messages deferred; decide again using query results.")
                    for request in decision.queries:
                        if not isinstance(request, CapabilityRequest):
                            raise ValueError("actor queries must be CapabilityRequest values")
                        self._consume_actor_tool()
                        self._actor_queries += 1
                        self.query(actor_id, request.capability, request.args)
                    continue
                if decision.validation_errors:
                    return self._actor_feedback(actor_id, runtime, "invalid_decision", "; ".join(decision.validation_errors))
                return self._apply_actor_decision(actor_id, runtime, decision)
            except ActorProcessingLimitError as exc:
                return self._actor_feedback(actor_id, runtime, "policy_budget_exhausted", str(exc))
            except Exception as exc:
                return self._actor_feedback(actor_id, runtime, "policy_error", str(exc) or type(exc).__name__)

    def _deliver_message(self, payload: Mapping[str, Any]) -> None:
        message_id = payload["message_id"]
        message = self._messages.get(message_id)
        if message is None:
            raise ValueError("message no longer exists")
        if message.sender_id != payload["sender_id"] or message.recipient_id != payload["recipient_id"]:
            raise ValueError("message identity mismatch")
        message.status = "delivered"
        self._get_actor(message.recipient_id).inbox.append(message)

    def schedule_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        delay: float = 0.0,
        precondition: EventPrecondition | None = None,
    ) -> str:
        event = self._schedule(delay, event_type, payload, precondition)
        return event.event_id

    def _schedule(
        self,
        delay: float,
        event_type: str,
        payload: dict[str, Any],
        precondition: EventPrecondition | None,
    ) -> ScheduledEvent:
        return self._scheduler.schedule_after(self.current_time, delay, event_type, payload, precondition)

    def step(
        self,
        action: Action | Mapping[str, Any],
        _actor_runtime_target: ActorRuntime | None = None,
    ) -> tuple[Any, float, bool, bool, dict[str, Any]]:
        before = self._state.to_dict()
        runtime_before = self._runtime_snapshot()
        outcome: str = "succeeded"
        reason = ""
        candidate = self._state.clone()
        random_samples: list[dict[str, Any]] = []
        context = ExecutionContext(self.current_time, self._random.source(random_samples), self._schedule)
        scheduler_snapshot = self._scheduler.snapshot()

        original_action = action
        if isinstance(action, Mapping):
            try:
                action = Action.from_dict(action)
            except Exception as exc:
                outcome, reason = "invalid", str(exc) or exc.__class__.__name__
                action_dict = deepcopy(dict(original_action))
        if not isinstance(action, Action):
            if not reason:
                outcome, reason = "invalid", "action must be an Action instance or mapping"
            action_dict = deepcopy(action) if isinstance(action, dict) else {"value": repr(action)}
        else:
            action_dict = action.to_dict()
            try:
                action.validate()
                if action.actor_id not in self._state.actors:
                    raise ActionExecutionError("actor does not exist")
                handler = self.task.operations.get(action.operation)
                if handler is None:
                    raise ActionExecutionError(f"unknown operation: {action.operation}")
                for policy in self.task.policies:
                    decision = policy.authorize(self._state.clone(), deepcopy(action))
                    if not decision.allowed:
                        outcome, reason = "unauthorized", decision.reason
                        break
                if outcome == "succeeded":
                    try:
                        if isinstance(handler, OperationDefinition):
                            handler.validate(action)
                            handler = handler.handler
                        _invoke(handler, candidate, action, context)
                        candidate._validate_indexes()
                    except Exception as exc:
                        outcome, reason = "invalid", str(exc) or exc.__class__.__name__
            except Exception as exc:
                outcome, reason = "invalid", str(exc) or exc.__class__.__name__

        if outcome != "succeeded":
            self._scheduler.restore(scheduler_snapshot)
        after = candidate.to_dict() if outcome == "succeeded" else before
        if outcome == "succeeded":
            self._state = candidate
        event = self._make_event(
            kind="action",
            sim_time=self.current_time,
            action=action_dict,
            outcome=outcome,
            reason=reason,
            before=before,
            after=after,
            random_samples=random_samples,
            scheduled_events=context.scheduled_events if outcome == "succeeded" else (),
            runtime_before=runtime_before,
            runtime_after=self._runtime_snapshot(),
        )
        self._update_termination(outcome)
        reward = (
            self.task.reward_fn(self._state, action, outcome == "succeeded")
            if self.task.reward_fn is not None and isinstance(action, Action)
            else (1.0 if self._terminated and outcome == "succeeded" else 0.0)
        )
        if _actor_runtime_target is not None:
            _actor_runtime_target.action_results.append(
                {
                    "action": deepcopy(action_dict),
                    "outcome": outcome,
                    "reason": reason,
                    "reward": reward,
                    "terminated": self._terminated,
                    "truncated": False,
                    "sim_time": self.current_time,
                }
            )
            event = self._make_event(
                kind="action",
                sim_time=self.current_time,
                action=action_dict,
                outcome=outcome,
                reason=reason,
                before=before,
                after=after,
                random_samples=random_samples,
                scheduled_events=context.scheduled_events if outcome == "succeeded" else (),
                runtime_before=runtime_before,
                runtime_after=self._runtime_snapshot(),
            )
        self._events.append(event)
        info = {"outcome": outcome, "reason": reason, "event": event.to_dict(), "seed": self._seed, "sim_time": self.current_time}
        return self._observation(), reward, self._terminated, False, info

    def advance_time(self, amount: float, max_events: int | None = None) -> tuple[dict[str, Any], ...]:
        """Advance simulated time and process all due events in stable order."""
        limit = self.max_events_per_advance if max_events is None else max_events
        if limit <= 0:
            raise ValueError("max_events must be positive")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
            raise ValueError("time advance must be a non-negative number")
        target_time = self.current_time + float(amount)
        self._autonomous_actions = 0
        self._actor_decisions = 0
        self._actor_queries = 0
        self._actor_tools = 0
        processed: list[dict[str, Any]] = []
        while self._scheduler.next_due_time() is not None and self._scheduler.next_due_time() <= target_time:
            next_due = self._scheduler.next_due_time()
            assert next_due is not None
            self._clock.advance_to(next_due)
            while self._scheduler.has_due(self.current_time):
                if len(processed) >= limit:
                    raise EventProcessingLimitError(f"more than {limit} events are due in one advance")
                scheduled = self._scheduler.pop_due(self.current_time)
                assert scheduled is not None
                event = self._execute_scheduled(scheduled)
                processed.append(event.to_dict())
                if event.outcome == "succeeded" and event.event_type == MESSAGE_EVENT_TYPE:
                    self._run_actor_once(event.event_payload["recipient_id"], enforce_budget=True)
        if self.current_time < target_time:
            self._clock.advance_to(target_time)
        return tuple(processed)

    def advance(self, amount: float, max_events: int | None = None) -> tuple[dict[str, Any], ...]:
        """Convenience alias for :meth:`advance_time`."""
        return self.advance_time(amount, max_events)

    def _execute_scheduled(self, scheduled: ScheduledEvent) -> TransitionEvent:
        before = self._state.to_dict()
        runtime_before = self._runtime_snapshot()
        random_samples: list[dict[str, Any]] = []
        context = ExecutionContext(self.current_time, self._random.source(random_samples), self._schedule)
        scheduler_snapshot = self._scheduler.snapshot()
        outcome: str = "succeeded"
        reason = ""
        candidate = self._state.clone()

        try:
            if scheduled.precondition is not None and not scheduled.precondition(self._state.clone(), scheduled):
                outcome, reason = "skipped", "event precondition was not satisfied"
            else:
                if scheduled.event_type == MESSAGE_EVENT_TYPE:
                    self._deliver_message(scheduled.payload)
                else:
                    handler = self.task.event_handlers.get(scheduled.event_type)
                    if handler is None:
                        outcome, reason = "failed", f"unknown event type: {scheduled.event_type}"
                    else:
                        _invoke(handler, candidate, scheduled, context)
                        candidate._validate_indexes()
        except Exception as exc:
            outcome, reason = "failed", str(exc) or exc.__class__.__name__

        if outcome != "succeeded":
            self._scheduler.restore(scheduler_snapshot)
        after = candidate.to_dict() if outcome == "succeeded" else before
        if outcome == "succeeded":
            self._state = candidate
        event = self._make_event(
            kind="scheduled",
            sim_time=self.current_time,
            action={},
            outcome=outcome,
            reason=reason,
            before=before,
            after=after,
            event_id=scheduled.event_id,
            event_type=scheduled.event_type,
            event_payload=scheduled.payload,
            event_order=scheduled.order,
            random_samples=random_samples,
            scheduled_events=context.scheduled_events if outcome == "succeeded" else (),
            runtime_before=runtime_before,
            runtime_after=self._runtime_snapshot(),
        )
        self._events.append(event)
        self._update_termination(outcome)
        return event

    def _make_event(self, **kwargs: Any) -> TransitionEvent:
        kwargs["state_before"] = kwargs.pop("before")
        kwargs["state_after"] = kwargs.pop("after")
        kwargs["state_changes"] = _state_changes(kwargs["state_before"], kwargs["state_after"])
        kwargs["sequence"] = len(self._events.events) + 1
        return TransitionEvent(seed=self._seed, **kwargs)

    def _update_termination(self, outcome: str) -> None:
        if outcome == "succeeded":
            self._terminated = bool(self.task.success_condition(self._state))

    def replay(self, records: Sequence[TransitionEvent | Mapping[str, Any]], seed: int | None = None) -> dict[str, Any]:
        """Reconstruct state from recorded outcomes without fresh handler/random execution."""
        normalized = [record.to_dict() if isinstance(record, TransitionEvent) else deepcopy(dict(record)) for record in records]
        recorded_seed = seed
        if recorded_seed is None and normalized:
            recorded_seed = normalized[0].get("seed")
        self.reset(recorded_seed)
        for expected in normalized:
            if expected.get("sequence") != len(self._events.events) + 1:
                raise ReplayError("event sequence is not contiguous")
            expected_time = float(expected.get("sim_time", 0.0))
            try:
                self._clock.set_for_replay(expected_time)
            except ValueError as exc:
                raise ReplayError(str(exc)) from exc
            if expected.get("state_before") != self.get_state():
                raise ReplayError(f"state-before mismatch at sequence {expected['sequence']}")
            if "runtime_before" in expected:
                expected_runtime = deepcopy(expected["runtime_before"])
                # Pre-stabilization messages had no receipt field. They remain
                # ordinary conversation when inspecting historical snapshots.
                for actor in expected_runtime.values():
                    for message in actor.get("inbox", []) + actor.get("outbox", []):
                        message.setdefault("action_receipt", None)
                if expected_runtime != self._runtime_snapshot():
                    raise ReplayError(f"runtime-before mismatch at sequence {expected['sequence']}")
            after = expected.get("state_after")
            try:
                reconstructed = WorldState.from_dict(after)
            except (KeyError, TypeError, ValueError) as exc:
                raise ReplayError(f"invalid state-after at sequence {expected['sequence']}: {exc}") from exc
            expected_changes = tuple(StateChange(**change) for change in expected.get("state_changes", []))
            actual_changes = _state_changes(expected["state_before"], after)
            if tuple(change.to_dict() for change in expected_changes) != tuple(change.to_dict() for change in actual_changes):
                raise ReplayError(f"state-change mismatch at sequence {expected['sequence']}")
            event = TransitionEvent.from_dict(expected)
            self._state = reconstructed
            if "runtime_after" in expected:
                self._restore_runtime(event.runtime_after)
            self._events.append(event)
            if event.kind == "scheduled" and event.event_id is not None:
                self._scheduler.remove(event.event_id)
            for scheduled in event.scheduled_events:
                self._scheduler.schedule_record(scheduled)
            self._update_termination(event.outcome)
        return self.get_state()

    def _observation(self) -> Any:
        return deepcopy(self.task.observation_fn(self._state.clone()))
