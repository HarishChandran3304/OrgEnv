"""Vendor the framework and the task into the Harbor task's two images (runtime and verifier).

Run from the repository root before `harbor run`:
    python -m tasks.relaypay_workday.eval.package_harbor

The copies are build inputs, not sources: they are git-ignored and rebuilt by this script.
"""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TASK = Path("tasks/relaypay_workday")
HARBOR = ROOT / TASK / "harbor"


def sources():
    found = [ROOT / "orgenv/__init__.py", ROOT / "orgenv/task.py", *sorted((ROOT / "orgenv/core").glob("*.py")),
             *sorted((ROOT / TASK).glob("*.py")), *sorted((ROOT / TASK / "cards").glob("*.py")),
             *sorted((ROOT / TASK / "service").glob("*.py"))]
    return [p.relative_to(ROOT) for p in found]


def main():
    files = sources()
    for target in (HARBOR / "environment/runtime/vendor", HARBOR / "tests/vendor"):
        if target.exists():
            shutil.rmtree(target)
        for rel in files:
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, dest)
    workspace = HARBOR / "environment/billing_service"  # what the agent is given to repair
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir()
    for path in sorted((ROOT / TASK / "service").glob("*.py")):
        shutil.copy2(path, workspace / path.name)
    print(f"vendored {len(files)} files into the runtime and verifier images")


if __name__ == "__main__":
    main()
