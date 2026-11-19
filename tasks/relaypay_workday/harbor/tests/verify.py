"""Only reads sidecar-origin evidence, never agent-written reward/status files."""
import json
from pathlib import Path
from tasks.relaypay_workday.verifier import verify_envelope, failure_details

try:
    envelope = json.loads(Path('/state/evidence.json').read_text())
    key = Path('/state/verifier-key').read_bytes()
    result = verify_envelope(envelope, key)
except Exception as exc:
    result = failure_details('trusted artifact load failed: ' + type(exc).__name__)
# This is the only scalar reward file. Details are explanatory, not a second score.
Path('/logs/verifier/reward-details.json').write_text(json.dumps(result, indent=2, allow_nan=False))
Path('/logs/verifier/reward.txt').write_text(format(result['reward'], '.12g') + '\n')
print(json.dumps({k: result.get(k) for k in ('seed', 'variant', 'scorecard', 'reward', 'reward_graded', 'resolved', 'checks', 'harm', 'error')}))
