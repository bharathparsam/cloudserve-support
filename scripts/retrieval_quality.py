"""Offline MiniLM representation comparison on trusted development calibration."""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from scripts.train_quality import load_development, short_text
from scripts.train import text
from src.retrieve import MODEL, REVISION, encode_queries


def support_metrics(sims, rows, passages, floor):
    truth = np.asarray([bool(r["labels"]["answerable_from_docs"]) for r in rows])
    order = np.argsort(-sims, axis=1, kind="stable")[:, :4]
    raw_hits, supported_hits = [], []
    for i, row in enumerate(rows):
        if truth[i]:
            expected = set(row["labels"]["expected_doc_ids"])
            raw_hits.append(bool(expected & {passages[j]["doc_id"] for j in order[i]}))
            supported_hits.append(
                bool(
                    expected
                    & {passages[j]["doc_id"] for j in order[i] if sims[i, j] >= floor}
                )
            )
    accepted = sims.max(axis=1) >= floor
    return {
        "answerable_count": int(truth.sum()),
        "no_support_count": int((~truth).sum()),
        "raw_expected_doc_hit_at_4": float(np.mean(raw_hits)),
        "qualified_expected_doc_recall": float(np.mean(supported_hits)),
        "answerable_acceptance": float(accepted[truth].mean()),
        "no_support_specificity": float((~accepted[~truth]).mean()),
        "floor": float(floor),
        "proxy_not_semantic_truth": True,
    }


def select_floor(sims, rows, passages):
    # Require no-support specificity >= original measured 23/30. Coverage cannot
    # improve by merely accepting more known unsupported development records.
    candidates = []
    for floor in sorted(set(sims.max(axis=1).tolist())):
        m = support_metrics(sims, rows, passages, floor)
        if m["no_support_specificity"] >= 23 / 30:
            candidates.append(m)
    return max(
        candidates,
        key=lambda m: (
            m["qualified_expected_doc_recall"],
            m["no_support_specificity"],
            m["floor"],
        ),
    )


def run(a):
    out = Path(a.output)
    if out.exists() and any(out.iterdir()):
        raise ValueError("Output must be empty; retain previous evidence")
    out.mkdir(parents=True, exist_ok=True)
    rows, split = load_development(a.development, a.models)
    root = Path(a.corpus).parent
    manifest = json.loads((root / "index-manifest.json").read_text())
    passages = json.loads((root / "passages.json").read_text())
    from sentence_transformers import SentenceTransformer

    snapshot = (
        Path(a.cache)
        / "hub"
        / ("models--" + MODEL.replace("/", "--"))
        / "snapshots"
        / REVISION
    )
    if not snapshot.is_dir():
        raise RuntimeError(
            "Cached pinned embedding snapshot required; no downloads allowed"
        )
    model = SentenceTransformer(str(snapshot), device="cpu", local_files_only=True)
    variants = {
        "original": np.load(root / "passage-embeddings.npy", allow_pickle=False),
        "title_content_all_tokens": encode_queries(
            model, [p["title"] + "\n" + p["content"] for p in passages]
        ),
    }
    report = {
        "protocol": "development calibration selection only; development-check reused for diagnostics; proxies not semantic support",
        "data_hash": split["data_hash"],
        "comparisons": [],
    }
    sets = {}
    for name in ("calibration", "check"):
        selected = [rows[i] for i in split[name]]
        sets[name] = (
            selected,
            encode_queries(model, [text(r) for r in selected]),
            encode_queries(model, [short_text(r) for r in selected]),
        )
    for name, emb in variants.items():
        selected, full, short = sets["calibration"]
        sims = full @ emb.T
        measured = select_floor(sims, selected, passages)
        item = {"representation": name, "calibration": measured}
        check, cf, cs = sets["check"]
        item["reused_check_full"] = support_metrics(
            cf @ emb.T, check, passages, measured["floor"]
        )
        item["calibration_subject"] = support_metrics(
            short @ emb.T, selected, passages, measured["floor"]
        )
        item["reused_check_subject"] = support_metrics(
            cs @ emb.T, check, passages, measured["floor"]
        )
        report["comparisons"].append(item)
        np.save(out / f"{name}.npy", emb, allow_pickle=False)
    report["baseline_frozen_floor_calibration"] = support_metrics(
        sets["calibration"][1] @ variants["original"].T,
        sets["calibration"][0],
        passages,
        manifest["retrieval_floor"],
    )
    (out / "experiment.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--development", required=True)
    p.add_argument("--models", default="assets/models")
    p.add_argument("--corpus", default="assets/corpus/documentation.json")
    p.add_argument("--cache", default="storage/cache/huggingface")
    p.add_argument("--output", required=True)
    run(p.parse_args())
