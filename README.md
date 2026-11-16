# OrgEnv

OrgEnv is a small deterministic framework for building environments in which an agent has to
work inside an organization: other people hold the authority, the information and the time
the agent needs, and they act on their own responsibilities.

The repository has two parts:

- [`orgenv/`](orgenv): the framework. It is domain-independent and has one dependency
  (`jsonschema`).
- [`tasks/`](tasks): tasks built on it. The first is
  [Midnight Billing Cascade](tasks/midnight_billing_cascade), a payment incident an on-call
  engineer has to contain, repair and recover from, with recorded runs of three models.

## Install and test

Python 3.11 or later.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest            # framework and task tests; the task tests take several minutes
.venv/bin/pytest tests      # framework only
.venv/bin/python -m examples.approval_workflow.main
```

## What the framework provides

| Concept | Where | What it does |
|---|---|---|
| World state | `core/models.py` | Typed `Actor` and `Resource` records plus task data. |
| Actions | `core/actions.py`, `core/tool_schema.py` | Named operations with JSON Schema arguments, validated before they run. |
| Authorization | `core/policies.py` | Composable policies (`RolePolicy`, `ApprovalRequiredPolicy`, or your own) evaluated on every action. |
| Transitions | `core/engine.py` | A handler runs on a deep copy of the state; the copy is committed only if validation passes. Rejected actions leave the state untouched and are logged. |
| Time | `core/clock.py`, `core/scheduler.py` | A simulated clock. `advance_time` processes due events in order without sleeping. |
| Randomness | `core/randomness.py` | Named streams derived from the episode seed, so sampling one mechanism does not shift another. |
| Actors | `core/actors.py`, `core/runtime.py` | Each actor has a policy, private knowledge, an inbox and its own view of the world. Messages are delivered through the scheduler. |
| Capabilities | `core/capabilities.py` | Read-only tools an actor may query, with their own authorization. |
| Event log and replay | `core/events.py`, `core/engine.py` | Every transition records before and after state. `replay` restores state from the log and checks it. |
| LLM actors | `core/llm_policy.py`, `core/model_client.py` | An actor policy backed by a model, with strict validation of what the model returns. Optional. |

A task supplies a `TaskDefinition` (`orgenv/task.py`): an initial state, operation handlers,
event handlers, authorization policies, an observation function and a success condition.

```python
from orgenv import Action, OrgEnv

env = OrgEnv(task)
env.reset(seed=7)
env.step(Action("alex", "request_change", {"resource_id": "prod-config", "value": "v2"}))
env.advance_time(1.0)
print(env.actor_observation("alex"))
```

[`examples/approval_workflow`](examples/approval_workflow) is a complete small task: a change
request that needs an approver, with a deterministic approver and an offline fake-LLM one.

### Guarantees and limits

- **Deterministic.** The same seed and the same actions give the same episode. Live model
  calls are not deterministic.
- **Observations are filtered.** An actor sees only public data, the resources it is allowed
  to see, its own knowledge, its inbox and the results of its own actions.
- **Advertising a tool grants nothing.** Every action and query is authorized again when it
  runs. Message text never grants permission.
- **Bounded.** Events per time advance and actor decisions per activation are capped, so a
  zero-delay loop cannot hang an episode.
- **Not a sandbox.** Task handlers and policies are trusted Python. A task that runs
  untrusted code must isolate it itself, as Midnight Billing Cascade does.
- **Replay checks recorded outcomes.** It does not re-run handlers. A task that needs
  stronger verification re-executes transitions itself; Midnight Billing Cascade does.

## Repository layout

```
orgenv/                         the framework
examples/approval_workflow/     a minimal task
tests/                          framework tests
tasks/midnight_billing_cascade/ the first task: simulation, Harbor package, evaluation tools, traces
```
