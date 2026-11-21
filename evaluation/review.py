"""Summarize actual independent sheets. Blank sheets produce unavailable metrics."""

import argparse, csv, json
from pathlib import Path

SCORES = ("support_1_5", "correctness_1_5", "clarity_1_5", "uncertainty_1_5")


def read(path):
    rows = list(csv.DictReader(Path(path).open()))
    reviewed = {}
    for row in rows:
        values = [row.get(k, "").strip() for k in SCORES]
        if not any(values):
            continue
        if not all(values):
            raise ValueError("Partial review score row; finish it before importing")
        scores = [int(x) for x in values]
        if not all(1 <= x <= 5 for x in scores):
            raise ValueError("Scores must be 1–5")
        if not row.get("reviewer", "").strip():
            raise ValueError("Actual reviewer identity required")
        rid = row.get("record_id")
        if not rid or rid in reviewed:
            raise ValueError("Review record IDs must be present and unique")
        satisfaction = row.get("satisfaction_1_5", "").strip()
        unsupported = row.get("unsupported_claims", "").strip()
        if satisfaction and not 1 <= int(satisfaction) <= 5:
            raise ValueError("Satisfaction score must be 1–5")
        if unsupported and int(unsupported) < 0:
            raise ValueError("Unsupported claim count must be nonnegative")
        reviewed[rid] = {
            "reviewer": row["reviewer"],
            "scores": scores,
            "quality": sum(scores) / 20 * 100,
            "satisfaction": int(satisfaction) if satisfaction else None,
            "unsupported_claims": int(unsupported) if unsupported else None,
        }
    return rows, reviewed


def summarize(path, second=None):
    rows, reviewed = read(path)
    quality = [x["quality"] for x in reviewed.values()]
    sat = [
        x["satisfaction"] for x in reviewed.values() if x["satisfaction"] is not None
    ]
    result = {
        "rows": len(rows),
        "reviewed": len(quality),
        "quality_mean": sum(quality) / len(quality) if quality else None,
        "satisfaction_proxy": sum(sat) / len(sat) if sat else None,
        "satisfaction_denominator": len(sat),
        "independent_review": "supplied" if quality else "unavailable",
        "two_assessor_agreement": None,
    }
    if second:
        _, other = read(second)
        common = set(reviewed) & set(other)
        if any(
            reviewed[x]["reviewer"].strip().lower()
            == other[x]["reviewer"].strip().lower()
            for x in common
        ):
            raise ValueError("Two independent assessor identities required")
        result["two_assessor_agreement"] = {
            "paired_responses": len(common),
            "all_four_scores_exact_agreement": (
                sum(reviewed[x]["scores"] == other[x]["scores"] for x in common)
                / len(common)
                if common
                else None
            ),
            "mean_absolute_score_difference": (
                sum(
                    sum(
                        abs(a - b)
                        for a, b in zip(reviewed[x]["scores"], other[x]["scores"])
                    )
                    for x in common
                )
                / (4 * len(common))
                if common
                else None
            ),
            "reviewers_50_response_target_met": len(common) >= 50,
            "disagreements_retained": sum(
                reviewed[x]["scores"] != other[x]["scores"] for x in common
            ),
            "note": "Scores were supplied by users; this import cannot establish real-world independence or authenticity.",
        }
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--second")
    a = p.parse_args()
    print(json.dumps(summarize(a.input, a.second), indent=2))
