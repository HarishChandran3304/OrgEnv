"""Run deterministic and offline-LLM approval workflows.

Usage: python3 -m examples.approval_workflow.main
"""

from __future__ import annotations

import json

from orgenv import (
    Action,
    ActionExecutionError,
    Actor,
    ActorDecision,
    ActorSpec,
    ApprovalRequiredPolicy,
    CapabilityDefinition,
    CapabilityRequest,
    DeterministicPolicy,
    FakeModelClient,
    ActorProfile,
    LLMActorPolicy,
    MessageRequest,
    OrgEnv,
    Resource,
    RolePolicy,
    TaskDefinition,
    WorldState,
)


def build_environment(policy_kind: str = "deterministic", model_client=None) -> OrgEnv:
    state = WorldState(
        actors={
            "alex": Actor("alex", "requester"),
            "priya": Actor("priya", "approver"),
        },
        resources={
            "change-1": Resource(
                "change-1",
                "change",
                owner_id="alex",
                state={"requested_value": "off", "approved": False, "value": "off"},
            )
        },
        task_data={"public": {"organization": "Example Corp", "process": "changes require approval"}},
    )

    def request_change(world: WorldState, action: Action) -> None:
        resource = world.get_resource(action.payload["resource_id"])
        resource.state["requested_value"] = action.payload.get("value")
        # A new request invalidates approval of the old value.
        resource.state["approved"] = False
        resource.state.pop("approved_value", None)
        resource.state["applied"] = False
        resource.state["requested"] = True

    def require_complete_request(resource) -> None:
        value = resource.state.get("requested_value")
        if not resource.state.get("requested") or not isinstance(value, str) or not value.strip():
            raise ActionExecutionError("Approval requires a submitted request with a non-empty requested value.")

    def approve_change(world: WorldState, action: Action) -> None:
        resource = world.get_resource(action.payload["resource_id"])
        require_complete_request(resource)
        resource.state["approved"] = True
        resource.state["approved_value"] = resource.state["requested_value"]

    def apply_change(world: WorldState, action: Action) -> None:
        resource = world.get_resource(action.payload["resource_id"])
        require_complete_request(resource)
        if not resource.state.get("approved") or resource.state.get("approved_value") != resource.state["requested_value"]:
            raise ActionExecutionError("The current requested value must be approved before applying.")
        resource.state["value"] = resource.state["requested_value"]
        resource.state["applied"] = True

    def get_approval_requirement(world: WorldState, actor_id: str, args: dict) -> dict:
        return {"requirement": "Owner must submit a request with a non-empty string requested_value before approval. Missing, null, or blank values cannot be approved."}

    def priya_policy(observation):
        inbox = observation["inbox"]
        resource = observation["resources"].get("change-1", {})
        if inbox and not observation["capability_results"]:
            return ActorDecision(
                queries=(CapabilityRequest("get_approval_requirement", {}),)
            )
        if inbox and observation["capability_results"] and resource.get("state", {}).get("requested") and not resource["state"].get("approved"):
            value = resource["state"].get("requested_value")
            if not isinstance(value, str) or not value.strip():
                return ActorDecision(messages=(MessageRequest("alex", "Please provide the requested value."),))
            return ActorDecision(
                action=Action("priya", "approve_change", {"resource_id": "change-1"}),
                messages=(MessageRequest("alex", "Approved: the change can proceed."),),
            )
        return None

    task = TaskDefinition(initial_state=state)
    resource_arg = {"type": "string", "minLength": 1, "description": "Resource identifier, e.g. change-1. Use the key resource_id."}
    resource_schema = {"type": "object", "properties": {"resource_id": resource_arg},
                       "required": ["resource_id"], "additionalProperties": False}
    task.register_operation("request_change", request_change,
        description="Submit or update a request. Missing/null value is an incomplete draft; any update invalidates prior approval.",
        input_schema={"type": "object", "properties": {"resource_id": resource_arg,
            "value": {"type": ["string", "null"], "description": "Requested value. A non-empty string is mandatory before approval."}},
            "required": ["resource_id"], "additionalProperties": False})
    task.register_operation("approve_change", approve_change,
        description="Approve a submitted change only after checking get_approval_requirement. Requires a non-empty requested value; otherwise fails. Approver role required.",
        input_schema=resource_schema)
    task.register_operation("apply_change", apply_change,
        description="Apply the currently approved requested value. An incomplete or unapproved request cannot be applied.",
        input_schema=resource_schema)
    task.register_capability(
        CapabilityDefinition(
            "get_approval_requirement",
            get_approval_requirement,
            policy=RolePolicy("get_approval_requirement", {"approver"}),
            description="Read the mandatory approval requirement before deciding. No arguments. Restricted to approvers; does not approve anything.",
            input_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        )
    )
    task.add_policy(RolePolicy("request_change", {"requester"}))
    task.add_policy(RolePolicy("approve_change", {"approver"}))
    task.add_policy(ApprovalRequiredPolicy("apply_change"))
    if policy_kind == "llm":
        priya_policy = LLMActorPolicy(
            actor_id="priya",
            profile=ActorProfile(
                role="approver",
                responsibilities=("review requested changes", "communicate decisions"),
                behavioral_instructions="Retrieve the approval requirement using the registered capability before deciding. Check the current resource against it. Request missing information or approve a complete request and communicate the outcome; never bypass policy.",
                priorities=("safety", "accuracy"),
            ),
            client=model_client or FakeModelClient([]),
            model="offline-fake-approval",
            available_operations=("approve_change",),
            available_capabilities=("get_approval_requirement",),
        )
    else:
        priya_policy = DeterministicPolicy(priya_policy)

    task.register_actor(
        ActorSpec(
            "alex",
            visible_resource_ids=frozenset({"change-1"}),
        )
    )
    task.register_actor(
        ActorSpec(
            "priya",
            policy=priya_policy,
            private_knowledge={"team": "change approvals"},
            visible_resource_ids=frozenset({"change-1"}),
        )
    )
    task.success_condition = lambda world: world.get_resource("change-1").state.get("applied", False)
    return OrgEnv(task)


def run(seed: int = 11) -> dict:
    env = build_environment()
    env.reset(seed=seed)
    alex_before = env.actor_observation("alex")
    priya_before = env.actor_observation("priya")
    env.step(Action("alex", "request_change", {"resource_id": "change-1", "value": "maintenance"}))
    env.send_message("alex", "priya", "Please review change-1.", delay=1)
    delivered = env.advance_time(1)
    _, reward, terminated, _, apply_info = env.step(Action("alex", "apply_change", {"resource_id": "change-1"}))
    return {
        "seed": seed,
        "alex_knows_requirements": "approval_requirements" in alex_before["private_knowledge"],
        "priya_private_team": priya_before["private_knowledge"]["team"],
        "alex_can_see_priya_queries": bool(alex_before["capability_results"]),
        "priya_requirement_result": env.get_actor_runtime("priya")["capability_results"][0]["result"],
        "delivered_event_types": [event["event_type"] for event in delivered],
        "priya_inbox": env.get_actor_runtime("priya")["inbox"],
        "alex_inbox": env.get_actor_runtime("alex")["inbox"],
        "apply_outcome": apply_info["outcome"],
        "reward": reward,
        "terminated": terminated,
        "state": env.get_state(),
        "events": env.events,
    }


def run_llm(seed: int = 11) -> tuple[dict, FakeModelClient]:
    client = FakeModelClient(
        [
            {"queries": [{"capability": "get_approval_requirement", "args": {}}], "messages": [], "action": None},
            {
                "queries": [],
                "messages": [{"recipient_id": "alex", "content": "Approved: the change can proceed."}],
                "action": {"operation": "approve_change", "payload": {"resource_id": "change-1"}},
            },
        ]
    )
    env = build_environment("llm", client)
    env.reset(seed=seed)
    env.step(Action("alex", "request_change", {"resource_id": "change-1", "value": "maintenance"}))
    env.send_message("alex", "priya", "Please review change-1.", delay=1)
    delivered = env.advance_time(1)
    _, reward, terminated, _, apply_info = env.step(Action("alex", "apply_change", {"resource_id": "change-1"}))
    return {
        "model_calls": len(client.calls),
        "delivered_event_types": [event["event_type"] for event in delivered],
        "apply_outcome": apply_info["outcome"],
        "reward": reward,
        "terminated": terminated,
        "state": env.get_state(),
        "events": env.events,
    }, client


def main() -> None:
    result = run()
    replay_env = build_environment()
    replay_state = replay_env.replay(result["events"])
    print(json.dumps({"priya_private_team": result["priya_private_team"], "alex_can_see_priya_queries": result["alex_can_see_priya_queries"], "alex_can_see_it": result["alex_knows_requirements"], "priya_requirement_result": result["priya_requirement_result"]}, sort_keys=True))
    print(json.dumps({"delivered_event_types": result["delivered_event_types"], "apply_outcome": result["apply_outcome"], "terminated": result["terminated"]}))
    print(json.dumps({"replay_identical": replay_state == result["state"], "event_count": len(result["events"])}, sort_keys=True))
    llm_result, client = run_llm()
    llm_replay = build_environment("llm", FakeModelClient([]))
    print(json.dumps({"llm_model_calls": llm_result["model_calls"], "llm_apply_outcome": llm_result["apply_outcome"], "llm_terminated": llm_result["terminated"], "llm_replay_identical": llm_replay.replay(llm_result["events"]) == llm_result["state"]}, sort_keys=True))


if __name__ == "__main__":
    main()
