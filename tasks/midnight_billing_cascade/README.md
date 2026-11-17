# Midnight Billing Cascade

An on-call engineer, Alex, takes over a recurring-billing incident at a payments company.
Provider requests are timing out, two worker pools keep retrying, and customers are being
charged twice. Alex has to contain the incident, repair the billing service, repair the
affected invoices and resume billing, so that every invoice ends paid by exactly one charge.

The code repair is one part. The rest depends on other people:

- the second worker pool belongs to another team;
- the provider evidence a repair needs is held by an engineer who is off shift, and the
  emergency substitute is only complete if it is requested at the right time;
- support refunds customers on its own unless it is guided;
- some rules are not enforced by the tools, and breaking one fails the run;
- one requirement of the code is written in contracts that only account owners can read.

The agent's brief is [`harbor/instruction.md`](harbor/instruction.md). The mechanisms, the
variants and the scoring are described in [`DESIGN.md`](DESIGN.md).

## Layout

| Path | Contents |
|---|---|
| `task.py`, `model.py` | The simulation: people, tools, the provider ledger, seeded incident generation. |
| `service/` | The billing service the agent is given to repair. |
| `sandbox.py` | Runs submitted code out of process against the provider; public and per-seed validation. |
| `verifier.py`, `scoring.py` | Independent verification and the reward. |
| `server.py` | The HTTP boundary the agent's tools go through. |
| `variants.py` | Switches for each group of mechanisms. |
| `harbor/` | The task packaged for Harbor: instruction, containers, reference solution, verifier entry point. |
| `eval/` | Offline drivers, controls, the trial agent and runner, frozen seeds. |
| `tests/` | Task tests. |
| `traces/` | Recorded model runs. |

## Run it

Offline, with no containers (from the repository root):

```bash
.venv/bin/pytest tasks/midnight_billing_cascade                         # several minutes
.venv/bin/python -m tasks.midnight_billing_cascade.eval.sweep oracle linked frozen
.venv/bin/python -m tasks.midnight_billing_cascade.eval.sweep never_ask_terms spec frozen
```

Through Harbor, with the runtime, the agent and the verifier in separate containers:

```bash
pip install -e '.[trials]'
python -m tasks.midnight_billing_cascade.eval.package_harbor            # vendor sources into the images
ORGENV_VARIANT_SEED=1308265273 ORGENV_TASK_VARIANT=linked \
  harbor run -p tasks/midnight_billing_cascade/harbor -a oracle
```

A model trial, using the same agent loop as the recorded traces:

```bash
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY; see eval/run_trial.py for other routes
ORGENV_TRIAL_EFFORT=high python -m tasks.midnight_billing_cascade.eval.run_trial MODEL linked 1308265273
```

Results are written to `runs/` in this folder.

## Isolation

- The agent's container has no network and no task source. It reaches the runtime only
  through `orgenvctl`, which exposes Alex's tools and nothing else.
- Submitted code runs in a separate unprivileged process that cannot read the runtime's
  source, state or signing key, and reaches the provider only through a pipe the runtime owns.
- The verifier runs in its own container on a signed record. It re-executes the recorded
  transitions and re-runs validation on the installed release before it scores anything.
  Files, messages and status claims written by the agent are never inputs.

`DESIGN.md`, `eval/`, `harbor/solution/` and the verifier describe or contain the answers.
They are published for people studying the task, and none of them is copied into the agent's
container.

## Validation

Measured on the 20 frozen seeds in [`eval/seeds.json`](eval/seeds.json) unless a row says otherwise.
These figures predate the changes in [`CHANGES.md`](CHANGES.md), which lists the current ones and two further
variants (`consent`, `sealed`).

| Policy | Variant | Resolved | Mean reward |
|---|---|---|---|
| Reference solution | each of the 10 variants | 20 / 20 | 0.985 to 1.000 |
| Reference solution, 60 further seeds | `spec`, `linked` | 60 / 60 each | 0.993, 0.995 |
| Does nothing (one seed, through Harbor) | `linked` | 0 / 1 | 0.000 |
| Reference, never asks for contract terms | `spec` | 0 / 20 | 0.275 |
| Reference, assumes every contract releases | `spec` | 0 / 20 | 0.276 |
| Reference, asks account owners only | `spec` | 5 / 20 | 0.453 |

## Model runs

One run per model on variant `linked`, seed 1308265273, high reasoning effort. Full records
are in [`traces/`](traces). These runs were recorded before the changes in
[`CHANGES.md`](CHANGES.md) (brief wording, support timing, graded score) and are not
comparable with runs made after them.

| Model | Reward | Resolved | Technical | Model calls |
|---|---|---|---|---|
| Claude Opus 5.5 | 0.295 | no | 9 of 9 validation groups | 78 |
| GPT-6 Astra | 0.293 | no | 9 of 9 validation groups | 104 |
| Claude Fable 5.2 | 0.299 | no | 9 of 9 validation groups | 78 |

All three asked for every contract term at the start and wrote a correct fix.

- **Opus 5.5 and Astra** requested the one-time emergency export at t=0, before the charges
  it had to cover had settled, so the evidence was incomplete and the repair could not be
  finished safely. Each also charged three customers again after a refund.
- **Fable 5.2** requested the export at t=3, repaired the backlog, resumed billing and harmed
  no customer. When the support hold ended at t=5, support refunded the only charge on four
  legitimate invoices. Fable held those four out of billing and handed the decision to the
  incident commander instead of charging the customers again, so four invoices ended unpaid
  and the run is not resolved. The reference solution bills such invoices again and accepts
  the recorded harm (reward 0.983 on this seed).
