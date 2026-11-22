"""Offline development-only robustness experiment; never consumes supplied validation.

The existing development-check split has already been examined. Results from it
are explicitly reused diagnostics, not an untouched estimate of generalization.
"""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone
import shutil

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.frozen import FrozenEstimator
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, confusion_matrix, f1_score
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import FeatureUnion, Pipeline

from scripts.train import bins, text
from src.ingest import sanitize_text


def load_development(path, model_dir):
    """Verify exact authorized bytes before decoding, regardless of filename."""
    model_dir = Path(model_dir)
    manifest = json.loads((model_dir / "manifest.json").read_text())
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["data_hash"]:
        raise ValueError("Input does not match trusted development fingerprint")
    split = json.loads((model_dir / "split-manifest.json").read_text())
    if split["data_hash"] != manifest["data_hash"]:
        raise ValueError("Development split fingerprint mismatch")
    return json.loads(raw), split


def views(row):
    """Verbatim sanitized subject/body views, without labels or invented text."""
    identities = [row.get("customer_name", ""), row.get("customer_id", "")]
    subject = sanitize_text(row.get("subject", ""), identities).strip()
    body = sanitize_text(row["body"], identities).strip()
    return list(dict.fromkeys(v for v in (text(row).strip(), subject, body) if v))


def pipeline(kind="word_char", c=4):
    features = [("word", TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True))]
    if kind == "word_char":
        features.append(
            (
                "char",
                TfidfVectorizer(
                    analyzer="char_wb", ngram_range=(3, 5), min_df=2, sublinear_tf=True
                ),
            )
        )
    return Pipeline(
        [
            ("features", FeatureUnion(features)),
            ("model", LogisticRegression(C=c, max_iter=1500, random_state=42)),
        ]
    )


def expanded(rows, labels, use_views):
    xs, ys, weights = [], [], []
    for row, label in zip(rows, labels):
        row_views = views(row) if use_views else [text(row)]
        xs.extend(row_views)
        ys.extend([label] * len(row_views))
        weights.extend([1 / len(row_views)] * len(row_views))
    return xs, ys, np.asarray(weights)


def short_text(row):
    # Subject is actual supplied customer text, not a generated paraphrase.
    return views(row)[1] if len(views(row)) > 1 else views(row)[0]


def conflict_summary(rows, dimension):
    groups = defaultdict(Counter)
    for row in rows:
        groups[text(row)][row["labels"][dimension]] += 1
    contradictory = [counts for counts in groups.values() if len(counts) > 1]
    return {
        "records": len(rows),
        "unique_sanitized_texts": len(groups),
        "contradictory_text_groups": len(contradictory),
        "records_in_contradictory_groups": sum(sum(c.values()) for c in contradictory),
        "deterministic_exact_text_resubstitution_ceiling": sum(
            max(c.values()) for c in groups.values()
        )
        / len(rows),
        "ceiling_is_not_unseen_accuracy": True,
    }


def run(args):
    out = Path(args.output)
    if out.exists() and any(out.iterdir()):
        raise ValueError("Output must be empty; preserve previous experiments")
    out.mkdir(parents=True, exist_ok=True)
    rows, split = load_development(args.development, args.models)
    report = {
        "protocol": "development fitting-split CV selection; calibration separate; reused check diagnostics only",
        "data_hash": split["data_hash"],
        "dimensions": {},
    }
    for dimension in ("intent", "urgency"):
        train = [rows[i] for i in split["train"]]
        labels = np.asarray([r["labels"][dimension] for r in train])
        candidates = []
        for kind, augment, c in [
            ("word", False, 4),
            ("word_char", False, 4),
            ("word", True, 4),
            ("word_char", True, 4),
            ("word_char", True, 16),
        ]:
            full_scores, short_scores = [], []
            for fit, hold in StratifiedKFold(3, shuffle=True, random_state=42).split(
                train, labels
            ):
                model = pipeline(kind, c)
                x, y, weights = expanded([train[i] for i in fit], labels[fit], augment)
                model.fit(x, y, model__sample_weight=weights)
                held = [train[i] for i in hold]
                full_scores.append(
                    f1_score(
                        labels[hold],
                        model.predict([text(r) for r in held]),
                        average="macro",
                        zero_division=0,
                    )
                )
                short_scores.append(
                    f1_score(
                        labels[hold],
                        model.predict([short_text(r) for r in held]),
                        average="macro",
                        zero_division=0,
                    )
                )
            candidates.append(
                {
                    "features": kind,
                    "subject_body_views": augment,
                    "C": c,
                    "cv_full_macro_f1": float(np.mean(full_scores)),
                    "cv_subject_macro_f1": float(np.mean(short_scores)),
                }
            )
        # Robustness objective declared here; no check data participates in choice.
        chosen = max(
            candidates,
            key=lambda c: (
                min(c["cv_full_macro_f1"], c["cv_subject_macro_f1"]),
                c["cv_full_macro_f1"],
                -c["C"],
            ),
        )
        model = pipeline(chosen["features"], chosen["C"])
        x, y, weights = expanded(train, labels, chosen["subject_body_views"])
        model.fit(x, y, model__sample_weight=weights)
        calibration_rows = [rows[i] for i in split["calibration"]]
        cx, cy, cw = expanded(
            calibration_rows,
            [r["labels"][dimension] for r in calibration_rows],
            chosen["subject_body_views"],
        )
        calibrated = CalibratedClassifierCV(FrozenEstimator(model), method="sigmoid")
        # Each calibration view is an observed text view; all views have equal
        # weight. Raw-model fit already balances each original fitting record.
        calibrated.fit(cx, cy)
        check = [rows[i] for i in split["check"]]
        truth = np.asarray([r["labels"][dimension] for r in check])
        diagnostics = {}
        for view, texts in [
            ("full", [text(r) for r in check]),
            ("subject", [short_text(r) for r in check]),
        ]:
            cp = calibrated.predict_proba(texts)
            diagnostics[view] = {
                "classification": classification_report(
                    truth,
                    calibrated.classes_[cp.argmax(axis=1)],
                    output_dict=True,
                    zero_division=0,
                ),
                "reliability": bins(truth, cp, calibrated.classes_),
            }
        cal_truth = np.asarray([r["labels"][dimension] for r in calibration_rows])
        cal_full = calibrated.predict([text(r) for r in calibration_rows])
        import joblib

        baseline = joblib.load(Path(args.models) / f"{dimension}.joblib")["calibrated"]
        baseline_cal = baseline.predict([text(r) for r in calibration_rows])
        report["dimensions"][dimension] = {
            "candidates": candidates,
            "selected": chosen,
            "calibration_accuracy": float(np.mean(cal_full == cal_truth)),
            "baseline_calibration_accuracy": float(np.mean(baseline_cal == cal_truth)),
            "reused_check": diagnostics,
            "fitting_text_conflicts": conflict_summary(train, dimension),
            "all_development_conflicts_diagnostic_only": conflict_summary(
                rows, dimension
            ),
        }
        joblib.dump(
            {"raw": model, "calibrated": calibrated}, out / f"{dimension}.joblib"
        )
    if args.promote:
        promote_intent(args, rows, split, report, out)
    (out / "experiment.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                d: {
                    "selected": v["selected"],
                    "full_accuracy": v["reused_check"]["full"]["classification"][
                        "accuracy"
                    ],
                    "subject_accuracy": v["reused_check"]["subject"]["classification"][
                        "accuracy"
                    ],
                    "conflicts": v["all_development_conflicts_diagnostic_only"],
                }
                for d, v in report["dimensions"].items()
            },
            indent=2,
        )
    )


def promote_intent(args, rows, split, report, out):
    """Promote only CV-supported intent; retain urgency unless separately justified."""
    import joblib

    root = Path(args.models)
    backup = out / "prior-artifacts"
    backup.mkdir()
    for name in (
        "intent.joblib",
        "urgency.joblib",
        "manifest.json",
        "development-report.json",
        "split-manifest.json",
    ):
        shutil.copy2(root / name, backup / name)
    policy_path = Path(args.policy)
    shutil.copy2(policy_path, backup / "routing.json")
    policy = json.loads(policy_path.read_text())
    bundle = joblib.load(out / "intent.joblib")
    model = bundle["calibrated"]
    minimum = float(policy["intent_threshold"])
    cal = [rows[i] for i in split["calibration"]]
    # Count original calibration records, not augmented views, for sample minima.
    confidence, correct, predicted = [], [], []
    for row in cal:
        p = model.predict_proba(views(row))
        labels = model.classes_[p.argmax(axis=1)]
        confidence.append(float(p.max(axis=1).min()))
        correct.append(bool(np.all(labels == row["labels"]["intent"])))
        predicted.append(str(labels[0]))
    confidence, correct = np.asarray(confidence), np.asarray(correct)
    valid = []
    for floor in sorted({minimum, *confidence.tolist()}):
        chosen = confidence >= floor
        if floor >= minimum and chosen.sum() >= 10 and correct[chosen].mean() >= 0.9:
            valid.append((int(chosen.sum()), float(floor)))
    floor = max(valid)[1] if valid else 1.0
    mandatory = set(policy["sensitive_intents"])
    allow = []
    for intent in model.classes_:
        chosen = (np.asarray(predicted) == intent) & (confidence >= floor)
        if (
            intent not in mandatory
            and chosen.sum() >= 2
            and correct[chosen].mean() >= 0.85
        ):
            allow.append(str(intent))
    policy.update(
        version="development-2-subject-body-views",
        intent_threshold=floor,
        confidence_threshold=floor,
        eligible_intents=allow,
        supported_intents=allow,
    )
    policy["selection"] = {
        "split": "calibration",
        "minimum_global_sample": 10,
        "minimum_global_precision": 0.9,
        "minimum_per_intent_sample": 2,
        "selected_count": int((confidence >= floor).sum()),
        "selected_intent_accuracy": (
            float(correct[confidence >= floor].mean())
            if (confidence >= floor).any()
            else None
        ),
        "selection_unit": "original record; worst confidence and correctness across verbatim full/subject/body views",
        "previous_intent_threshold": minimum,
        "threshold_not_lowered": floor >= minimum,
        "limitation": "Correlated development templates; reused check diagnostics; not semantic quality or confidence-reliability guarantee",
    }
    shutil.copy2(out / "intent.joblib", root / "intent.joblib")
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["files"]["intent.joblib"] = hashlib.sha256(
        (root / "intent.joblib").read_bytes()
    ).hexdigest()
    manifest["artifact_id"] = hashlib.sha256(
        json.dumps(manifest["files"], sort_keys=True).encode()
    ).hexdigest()
    manifest.update(
        created_at=datetime.now(timezone.utc).isoformat(),
        features="intent: sanitized full/subject/body fitting views, per-record balanced TF-IDF word/char logistic regression; urgency retained original",
        calibration="intent sigmoid fitted on disjoint calibration text views; urgency original sigmoid; reliability measured not guaranteed",
        development_check_reused=True,
    )
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    previous = json.loads((root / "development-report.json").read_text())
    intent = report["dimensions"]["intent"]
    check = [rows[i] for i in split["check"]]
    truth = np.asarray([r["labels"]["intent"] for r in check])
    raw = bundle["raw"].predict_proba([text(r) for r in check])
    predictions = model.predict([text(r) for r in check])
    previous["dimensions"]["intent"].update(
        selected=intent["selected"],
        candidates=intent["candidates"],
        calibrated_check=intent["reused_check"]["full"]["classification"],
        calibrated_reliability=intent["reused_check"]["full"]["reliability"],
        raw_check=classification_report(
            truth,
            bundle["raw"].classes_[raw.argmax(axis=1)],
            output_dict=True,
            zero_division=0,
        ),
        raw_reliability=bins(truth, raw, bundle["raw"].classes_),
        confusion_matrix=confusion_matrix(
            truth, predictions, labels=model.classes_
        ).tolist(),
    )
    previous.update(
        development_check_reused=True,
        completion_quality_evidence=str(out / "experiment.json"),
        policy_selection=policy,
    )
    (root / "development-report.json").write_text(json.dumps(previous, indent=2))
    policy_path.write_text(json.dumps(policy, indent=2))
    report["promotion"] = {
        "intent": "promoted using fitting CV and calibration-only floor",
        "urgency": "retained original; contradictory labels and weak CV; no reliable gain established",
        "policy": policy["selection"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--development", required=True)
    parser.add_argument("--models", default="assets/models")
    parser.add_argument("--output", required=True)
    parser.add_argument("--policy", default="policies/routing.json")
    parser.add_argument("--promote", action="store_true")
    run(parser.parse_args())
