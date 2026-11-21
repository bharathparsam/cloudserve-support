"""Scan distributable source for configured secrets without printing their values."""

from pathlib import Path
import json, re
from dotenv import dotenv_values
from src.ingest import now

root = Path(__file__).resolve().parents[1]
secret_values = [
    v
    for k, v in dotenv_values(root / ".env").items()
    if k in ("OPENROUTER_API_KEY", "OPERATOR_TOKEN", "GRAFANA_ADMIN_PASSWORD") and v
]
findings = []
count = 0
for p in root.rglob("*"):
    relative = p.relative_to(root)
    if (
        any(
            x in relative.parts
            for x in (
                ".venv",
                "node_modules",
                "storage",
                ".git",
                ".local-notes",
                "dist",
                "__pycache__",
                ".pytest_cache",
                "test-results",
            )
        )
        or p.name.startswith(".env")
        or not p.is_file()
    ):
        continue
    try:
        text = p.read_text()
    except (UnicodeError, OSError):
        continue
    count += 1
    if any(v in text for v in secret_values) or re.search(
        r"sk-or-v1-[A-Za-z0-9]{32,}", text
    ):
        findings.append(str(relative))
report = {
    "timestamp": now(),
    "text_files_scanned": count,
    "findings_paths": findings,
    "history": (
        "Git repository detected; working-tree scan only, not a history audit"
        if (root / ".git").exists()
        else "No Git repository detected"
    ),
    "scope": "Distributable source; local environment/runtime protected artifacts excluded",
}
(root / "storage/secret-scan.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
if findings:
    raise SystemExit(1)
