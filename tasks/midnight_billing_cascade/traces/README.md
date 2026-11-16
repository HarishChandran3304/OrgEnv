# Traces

One run per model on variant `linked`, seed 1308265273, high reasoning effort, using the
agent loop in [`../eval/agent.py`](../eval/agent.py).

| Folder | Model | Reward | Resolved |
|---|---|---|---|
| `claude-opus-5-5/` | Claude Opus 5.5 | 0.295 | no |
| `gpt-6-astra/` | GPT-6 Astra | 0.293 | no |

Each folder holds:

- `trace.json`: one record per model call, with the model's reasoning summary, the tool call
  it chose, the result and the token usage. It also holds the system prompt, the
  instruction, the tool catalog and the first observation.
- `reward.json`: the verifier's full output, including every check, harm event and failed invoice.
- `rollout.json.gz`: the complete record, including the exact prompt of every call.

Notes on reading them:

- The runs were recorded before the project was renamed from OrgSim to OrgEnv, so the
  recorded text says `orgsimctl` and `orgsim-reward-v3`. Replaying each trace's tool calls in
  this repository gives the recorded reward
  (`python -m tasks.midnight_billing_cascade.eval.replay_trace`, on the unpacked rollout).
- Opus 5.5 and Astra were called through an OpenAI-compatible API relay, recorded as
  provider `relay`.
- Reasoning summaries are what the model API returned; they are not the model's full reasoning.
