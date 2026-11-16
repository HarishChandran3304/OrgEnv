# OrgEnv

OrgEnv is a framework for building environments that test whether an AI agent can get real
work done inside an organization, where other people hold the authority, the information and
the time the agent needs.

## Why

Most agent benchmarks hand the agent a well-specified task, the inputs and the permission to
act. Real work rarely arrives that way. An engineer handling a production incident has to find
out who knows what, get approvals, work around who is available, keep promises others have
made, and adapt while the situation keeps changing. An agent can be excellent at the code and
still fail at all of that.

This distinction is the subject of Mercor's work on organizational difficulty:

- **Blog:** [Playing Well With Others](https://www.mercor.com/blog/playing-well-with-others/)
- **Paper:** [Harder Than the Task: From Technical to Organizational Difficulty in AI Benchmarking](https://arxiv.org/abs/2611.03494)
  (Barres, Barton, Foss and Nielsen)

They separate **technical difficulty** (how hard the task is once the goal, the inputs and the
authority to act are given) from **organizational difficulty** (how the task relates to other
people and activities). The two are independent: a small bug fix can be organizationally hard.
Their survey of about 522 prominent benchmarks found that fewer than a quarter build in any
organizational dimension.

OrgEnv is an attempt to build tasks where organizational difficulty is real, has
consequences, and is verified from recorded state instead of from what the agent says.

## The six dimensions, and how a task uses them

The task in this repository, [Midnight Billing Cascade](tasks/midnight_billing_cascade), uses
all six dimensions from that work.

| Dimension | In the task |
|---|---|
| Task interdependence | The repair needs a reviewed plan, an approved release and evidence that other people produce. |
| Information structure | The provider ledger, the worker journals and the merchants' contract terms are each held by someone else, and some of what the agent can see is stale or wrong. |
| Authority structure | A second worker pool belongs to another team. Emergency powers exist only while the usual owner is away. Some rules are not enforced by the tools and are audited afterwards. |
| Temporal structure | A billing close, renewal grace periods, settlement delays and people's shifts all run on one clock. |
| Incentive structure | Support refunds customers on its own initiative. A pool owner will not stop a healthy pool. |
| Environmental dynamism | Workers keep retrying, customers keep complaining and updating cards, and the provider puts a merchant under review partway through. |

## What is here

- [`orgenv/`](orgenv): the framework. It is domain-independent and has one dependency
  (`jsonschema`).
- [`tasks/midnight_billing_cascade/`](tasks/midnight_billing_cascade): the first task. An
  on-call engineer has to contain a payment incident, repair the billing service, repair the
  affected invoices and resume billing. It comes packaged for Harbor, with a reference
  solution, controls and an independent verifier.
- [`tasks/midnight_billing_cascade/traces/`](tasks/midnight_billing_cascade/traces): recorded
  runs of three frontier models.

## How an episode works

1. A seed generates an incident: invoices, charges, people's availability and contract terms.
2. The agent acts through tools: it reads the systems it is allowed to see, messages people,
   edits and tests the code, submits repair plans and asks for approvals.
3. The other people are simulated. Each has their own information, availability and
   responsibilities, and acts on them whether or not the agent asks.
4. Time advances only when the agent lets it. Everything else keeps running while it passes.
5. A separate verifier re-executes the recorded actions and scores the outcome: the deployed
   code's behaviour, the money that moved, and the harm done to customers.

## First results

One run per model on the hardest variant (`linked`), same seed, high reasoning effort.

| Model | Reward | Resolved | Code repair | Customers harmed |
|---|---|---|---|---|
| Claude Fable 5.2 | 0.299 | no | correct | 0 |
| Claude Opus 5.5 | 0.295 | no | correct | 3 |
| GPT-6 Astra | 0.293 | no | correct | 3 |
| Reference solution | 0.983 | yes | correct | 2 |

All three models wrote a correct fix, and none resolved the incident. Each failure was
organizational: two asked for evidence before it could be complete, and one declined to bill
customers again after support had refunded them. One run per model is not a ranking. Details
are in the [task README](tasks/midnight_billing_cascade/README.md).

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
