"""Fingerprint actual source/artifacts/protocol without committing changes."""

import argparse, json, platform, sys, hashlib
from pathlib import Path
import numpy as np
from src.config import Settings
from src.ingest import now, digest, load_records
from evaluation.harness import tree_hash, configured_fingerprint


def freeze(settings, output, development_input=None):
    target = Path(output)
    if target.exists():
        raise ValueError(
            "Freeze exists. Preserve it; choose a separately named revision if changes are necessary."
        )
    files = {}
    for folder in ("assets/models", "assets/corpus", "policies", "prompts"):
        for p in sorted((settings.root / folder).rglob("*")):
            if p.is_file():
                files[str(p.relative_to(settings.root))] = hashlib.sha256(
                    p.read_bytes()
                ).hexdigest()
    median = None
    if development_input:
        records = load_records(development_input)
        bodies = [
            len(r["body"])
            for r in records
            if isinstance(r, dict) and isinstance(r.get("body"), str)
        ]
        median = float(np.median(bodies)) if bodies else None
    result = {
        "frozen_at": now(),
        "configured_artifacts": configured_fingerprint(settings),
        "configured_model": settings.model,
        "code_hash": tree_hash(settings.root),
        "dependency_hash": digest((settings.root / "requirements.txt").read_text()),
        "platform_dependency_locks": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in settings.root.glob("requirements-linux-*.txt")
        },
        "assets": files,
        "python": sys.version,
        "platform": platform.platform(),
        "human_review_sampling": "first 50 generated answers by input index, two independent sheets; fewer than50 requires additional development cohort, labelled separately",
        "quantiles": "numpy linear",
        "short_long_body_character_boundary": median,
        "short_long_definition": "short <= development median, long > median; unavailable if no development input supplied",
        "validation_cache": "entire model_cache cleared after exclusive pipeline lock before supplied validation",
        "no_commit": "User requested review before any commit; content fingerprints substitute for code commit ID.",
        "targets_file": "build-specs/10_EVALUATION.md (copied definitions in src/metrics.py)",
        "human_reviews": "Required actual independent reviewers; no scores populated by software",
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2))
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output")
    p.add_argument("--development-input")
    a = p.parse_args()
    from src.runtime import load_configuration

    s = load_configuration(Settings())
    target = Path(a.output) if a.output else s.storage / "freeze.json"
    freeze(s, target, a.development_input)
    print(str(target))


if __name__ == "__main__":
    main()
