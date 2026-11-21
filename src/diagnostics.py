"""Safe, actionable failure explanations; never release authority or model output."""


def provider_failure(provider, phase):
    metadata = getattr(provider, "metadata", {})
    status = metadata.get("last_http_status")
    error = metadata.get("last_error")
    if isinstance(error, str) and error.startswith("paid_budget"):
        return f"{phase} unavailable: paid-test budget is exhausted, reserved or blocked; inspect the budget in Settings. No fallback request was made."
    if status == 429:
        return (
            f"{phase} unavailable: model provider rate limited the request (HTTP 429)."
        )
    if status in (401, 402, 403):
        return f"{phase} unavailable: model provider rejected access (HTTP {status}); check local credentials and free access."
    if status in (400, 404, 422):
        return f"{phase} unavailable: model provider rejected the configured request (HTTP {status})."
    if isinstance(status, int) and status >= 500:
        return f"{phase} unavailable: model provider returned a service error (HTTP {status})."
    if error == "network_timeout":
        return f"{phase} unavailable: model provider connection timed out."
    if error == "incomplete_output" and metadata.get("last_finish_reason") == "length":
        return f"{phase} unavailable: model reached its output token limit before completing the required structured response."
    if error == "provider_refusal":
        return f"{phase} unavailable: model provider refused the structured response."
    if error in (
        "cost_policy_violation",
        "usage_cost_invalid",
        "unexpected_nonzero_cost",
        "usage_cost_missing",
    ):
        return f"{phase} unavailable: free-only cost verification failed; no paid fallback is permitted."
    if status == 200:
        return f"{phase} unavailable: provider output did not satisfy the required structured response checks."
    return f"{phase} unavailable: a verified model response could not be obtained."


def explain_route(decision, classification, assessment, raw_sources, provider):
    codes = decision.reason_codes
    if "sensitivity_unknown" in codes:
        if "provider_unavailable" in assessment.reasons:
            return decision.model_copy(
                update={"reason": provider_failure(provider, "Sensitivity assessment")}
            )
        return decision.model_copy(
            update={
                "reason": "Sensitivity assessment could not be confirmed; the request requires operator review."
            }
        )
    if "confidence_below_floor" in codes:
        score = classification.intent.calibrated_probability
        reason = f'Intent confidence {score:.3f} is below the required {decision.thresholds["intent"]:.3f}.'
        best = max((s.score for s in raw_sources), default=None)
        if best is not None and best < decision.thresholds["retrieval"]:
            reason += f' Best retrieved support {best:.3f} is also below the required {decision.thresholds["retrieval"]:.3f}.'
        return decision.model_copy(update={"reason": reason})
    if "retrieval_below_floor" in codes:
        best = max((s.score for s in raw_sources), default=None)
        reason = (
            f'Best retrieved support {best:.3f} is below the required {decision.thresholds["retrieval"]:.3f}.'
            if best is not None
            else "No resolvable documentation passage met the required support threshold."
        )
        return decision.model_copy(update={"reason": reason})
    return decision
