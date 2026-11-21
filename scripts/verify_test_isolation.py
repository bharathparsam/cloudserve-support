"""Run the documented container tests and prove the running development store was untouched."""

import json, sqlite3, subprocess
from pathlib import Path
from src.ingest import now

root = Path(__file__).resolve().parents[1]
db = root / "storage/decisions.db"


def snapshot():
    with sqlite3.connect(db) as c:
        return {
            "inputs": c.execute("SELECT count(*) FROM input_records").fetchone()[0],
            "outcomes": c.execute("SELECT count(*) FROM outcomes").fetchone()[0],
            "triggers": c.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
            ).fetchall(),
            "control": c.execute(
                "SELECT enabled,epoch FROM operator_controls"
            ).fetchone(),
        }


before = snapshot()
with (root / "storage/docker-tests-isolated.log").open("w") as log:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "run",
            "--rm",
            "--no-deps",
            "app",
            "python",
            "-m",
            "pytest",
            "tests/",
            "-q",
        ],
        cwd=root,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
after = snapshot()
report = {
    "timestamp": now(),
    "command": "docker compose run --rm --no-deps app python -m pytest tests/ -q",
    "exit_code": result.returncode,
    "before": before,
    "after": after,
    "store_unchanged": before == after,
}
(root / "storage/test-isolation-check.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
if result.returncode or before != after:
    raise SystemExit(1)
