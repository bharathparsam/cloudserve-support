"""Prepare a clean portable copy with fresh secrets/storage; leaves all evidence intact."""

from pathlib import Path
import tempfile, shutil, subprocess, sys, json

root = Path(__file__).resolve().parents[1]
target = Path(tempfile.mkdtemp(prefix="cloudserve-rehearsal-"))
shutil.copytree(
    root,
    target,
    dirs_exist_ok=True,
    ignore=shutil.ignore_patterns(
        ".git",
        ".local-notes",
        "evidence",
        "playwright-report",
        "blob-report",
        ".env",
        ".env.*",
        ".venv",
        "node_modules",
        "storage",
        "__pycache__",
        ".pytest_cache",
        "dist",
        "test-results",
        "input",
    ),
)
# Restore placeholder-only example excluded by wildcard; real .env is never copied.
shutil.copy2(root / ".env.example", target / ".env.example")
subprocess.run([sys.executable, str(target / "scripts/setup_secrets.py")], check=True)
inputs = target / "data/input"
inputs.mkdir(parents=True, exist_ok=True)
records = [
    {"channel": channel, "body": "How should I retry an HTTP 429 response?"}
    for channel in ("email", "chat", "docs_comment", "forum")
] + [
    None,
    {"ticket_id": "repeat", "channel": "chat", "body": "How do I retry HTTP 429?"},
    {"ticket_id": "repeat", "channel": "email", "body": "How do I retry HTTP 429?"},
]
(inputs / "renamed-seven.json").write_text(json.dumps(records, ensure_ascii=False))
(root / "storage/clean-rehearsal-location.txt").write_text(str(target) + "\n")
print(str(target))
