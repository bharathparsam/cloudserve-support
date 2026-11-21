"""Explicit, bounded paid development tests; never selected by production settings.

Budget belongs to this single test adapter. The caller must not create another
adapter to replenish it. Unreported attempts retain their worst-case reservation.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json

from .openrouter import OpenRouterProvider, ProviderUnavailable, FreePolicyError


class PaidTestProvider(OpenRouterProvider):
    allowed_models = {
        "openai/gpt-4.1-mini": (Decimal("0.0000004"), Decimal("0.0000016")),
        "openai/gpt-4o-mini": (Decimal("0.00000015"), Decimal("0.0000006")),
    }
    maximum_requests = 3
    maximum_request_bytes = 262144
    framing_token_allowance = 4096

    def __init__(
        self,
        api_key,
        model="openai/gpt-4.1-mini",
        budget_usd=Decimal(".25"),
        timeout=30,
        max_retries=0,
    ):
        try:
            budget = Decimal(str(budget_usd))
        except (ValueError, InvalidOperation):
            raise ValueError("Invalid paid test budget") from None
        if (
            isinstance(budget_usd, bool)
            or not budget.is_finite()
            or not 0 < budget <= Decimal(".25")
        ):
            raise ValueError("Paid test budget must be positive and at most USD 0.25")
        if (
            model not in self.allowed_models
            or isinstance(max_retries, bool)
            or max_retries != 0
        ):
            raise ValueError(
                "Explicit allowed paid test model and zero retries required"
            )
        super().__init__(api_key=api_key, model=model, timeout=timeout, max_retries=0)
        self.budget_usd = budget
        self._reported_cost = Decimal(0)
        self._unreported_reservations = Decimal(0)
        self._current_reservation = Decimal(0)
        self._paid_requests = 0
        self._rates = None
        self.metadata.update(
            cost_policy="paid_test_only",
            test_only=True,
            max_paid_requests=self.maximum_requests,
        )
        self._budget_metadata()

    def _budget_metadata(self):
        self.metadata.update(
            budget_usd=str(self.budget_usd),
            reported_cost_usd=str(self._reported_cost),
            cumulative_cost_usd=str(self._reported_cost),
            reserved_unreported_cost_usd=str(self._unreported_reservations),
            budget_remaining_usd=str(
                max(
                    Decimal(0),
                    self.budget_usd
                    - self._reported_cost
                    - self._unreported_reservations,
                )
            ),
            paid_request_count=self._paid_requests,
        )

    def _prices(self, pricing):
        if (
            not isinstance(pricing, dict)
            or not {"prompt", "completion"} <= pricing.keys()
        ):
            raise FreePolicyError("Paid test prices unavailable")
        amounts = {}
        for name, value in pricing.items():
            if isinstance(value, bool):
                raise FreePolicyError("Paid test prices invalid")
            try:
                amount = Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError):
                raise FreePolicyError("Paid test prices invalid") from None
            if not amount.is_finite() or amount < 0:
                raise FreePolicyError("Paid test prices invalid")
            amounts[name] = amount
        limits = self.allowed_models[self.model]
        if amounts["prompt"] > limits[0] or amounts["completion"] > limits[1]:
            raise FreePolicyError("Paid test token rate exceeds approved ceiling")
        for name, amount in amounts.items():
            if name in ("prompt", "completion"):
                continue
            if (
                name in ("input_cache_read", "input_cache_write")
                and amount <= amounts["prompt"]
            ):
                continue
            # Web search is never enabled by this adapter; reject it in requests.
            if name == "web_search" and amount <= Decimal(".01"):
                continue
            if amount != 0:
                raise FreePolicyError("Unsupported paid test charge")
        return amounts["prompt"], amounts["completion"]

    def preflight(self):
        if self._cost_violation:
            raise FreePolicyError("Paid test budget policy latched")
        self._verified = False
        self.metadata.update(verified=False, catalog_verified=False)
        if self.model not in self.allowed_models:
            raise FreePolicyError("Paid test model not explicitly allowed")
        if not self.api_key:
            self.metadata["status"] = "missing_key"
            raise ProviderUnavailable("OpenRouter key missing")
        headers = {"Authorization": "Bearer " + self.api_key}
        try:
            response = self.client.get("/models", headers=headers)
            response.raise_for_status()
            model = next(
                (m for m in response.json()["data"] if m.get("id") == self.model), None
            )
            if not model:
                raise FreePolicyError("Explicit paid test model unavailable")
            self._prices(model.get("pricing"))
            architecture = model.get("architecture", {})
            if "text" not in architecture.get(
                "input_modalities", []
            ) or "text" not in architecture.get("output_modalities", []):
                raise FreePolicyError("Paid test text input/output unavailable")
            response = self.client.get(
                "/models/" + self.model + "/endpoints", headers=headers
            )
            response.raise_for_status()
            endpoints = response.json().get("data", {}).get("endpoints", [])
            verified = []
            for endpoint in endpoints:
                if endpoint.get("provider_name") not in ("OpenAI", "Azure"):
                    continue
                try:
                    rates = self._prices(endpoint.get("pricing"))
                except FreePolicyError:
                    continue
                verified.append((endpoint, rates))
            if not verified:
                raise FreePolicyError("No bounded paid test endpoint")
            self._rates = tuple(max(r[i] for _, r in verified) for i in (0, 1))
            self.providers = sorted({e["provider_name"] for e, _ in verified})
            identities = {self.model, model.get("canonical_slug")}
            for endpoint, _ in verified:
                identities.update((endpoint.get("model_id"), endpoint.get("model")))
                name = endpoint.get("name")
                if isinstance(name, str) and "|" in name:
                    identities.add(name.rsplit("|", 1)[1].strip())
            self._model_ids = self._aliases(identities)
            params = set(model.get("supported_parameters", []))
            self.response_format = "response_format" in params and all(
                "response_format" in e.get("supported_parameters", params)
                for e, _ in verified
            )
            self.temperature = "temperature" in params
            reasoning = model.get("reasoning")
            mandatory = (
                reasoning.get("mandatory") if isinstance(reasoning, dict) else None
            )
            self.reasoning_config = (
                {"enabled": False}
                if mandatory is False and "reasoning" in params
                else None
            )
            self.metadata.update(
                verified=True,
                catalog_verified=True,
                status="verified",
                preflight_status="verified",
                verified_at=datetime.now(timezone.utc).isoformat(),
                catalog_hash=hashlib.sha256(
                    json.dumps(model, sort_keys=True).encode()
                ).hexdigest(),
                model_pricing=model["pricing"],
                verified_model_ids=sorted(self._model_ids),
                verified_endpoints=[
                    {"provider": e["provider_name"], "pricing": e["pricing"]}
                    for e, _ in verified
                ],
                routing_price_limits=self._routing_price_limits(),
                response_format=self.response_format,
                strict_schema=False,
                reasoning_request_config=self.reasoning_config,
            )
            self._verified = True
            return dict(self.metadata)
        except FreePolicyError:
            self.metadata["status"] = "paid_test_policy_rejected"
            raise
        except Exception:
            self.metadata["status"] = "preflight_unavailable"
            raise ProviderUnavailable("Paid test preflight unavailable") from None

    def _routing_price_limits(self):
        if self._rates is None:
            raise FreePolicyError("Paid test prices not verified")
        return dict(
            zip(
                ("prompt", "completion"),
                (float(rate * 1000000) for rate in self._rates),
            )
        )

    def _before_request(self, request):
        if self._paid_requests >= self.maximum_requests:
            self._failure("paid_test_request_limit")
            raise ProviderUnavailable("Paid test request limit reached")
        if (
            any(
                name in request
                for name in ("tools", "plugins", "web_search_options", "models")
            )
            or request.get("model") != self.model
        ):
            self._failure("paid_test_configuration_rejected")
            raise FreePolicyError("Paid test optional capabilities forbidden")
        size = len(
            json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
        if size > self.maximum_request_bytes or self._rates is None:
            self._failure("paid_test_request_too_large")
            raise ProviderUnavailable("Paid test request exceeds bounded size")
        reservation = (size + self.framing_token_allowance) * self._rates[0] + request[
            "max_tokens"
        ] * self._rates[1]
        if (
            self._reported_cost + self._unreported_reservations + reservation
            > self.budget_usd
        ):
            self._failure("paid_test_budget_insufficient")
            raise ProviderUnavailable("Paid test remaining budget insufficient")
        self._current_reservation = reservation
        self._unreported_reservations += reservation
        self._paid_requests += 1
        self.metadata["last_reserved_cost_usd"] = str(reservation)
        self._budget_metadata()

    def _paid_cost_failure(self, code):
        self._cost_violation = True
        self._verified = False
        self.metadata["verified"] = False
        self._failure(code)
        self._budget_metadata()
        raise FreePolicyError("Paid test usage cost or budget unverified")

    def _capture_usage(self, data):
        raw = data.get("usage")
        safe = {
            key: raw[key]
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if isinstance(raw, dict)
            and isinstance(raw.get(key), int)
            and not isinstance(raw[key], bool)
            and raw[key] >= 0
        }
        safe["cost"] = None
        self.metadata["usage"] = safe
        if not isinstance(raw, dict) or "cost" not in raw:
            self._paid_cost_failure("usage_cost_missing")
        try:
            cost = Decimal(str(raw["cost"]))
            if isinstance(raw["cost"], bool) or not cost.is_finite() or cost < 0:
                raise ValueError()
        except (InvalidOperation, TypeError, ValueError):
            self._paid_cost_failure("usage_cost_invalid")
        safe["cost"] = str(cost)
        self._reported_cost += cost
        self._unreported_reservations -= self._current_reservation
        reservation = self._current_reservation
        self._current_reservation = Decimal(0)
        self._budget_metadata()
        if (
            cost > reservation
            or self._reported_cost + self._unreported_reservations > self.budget_usd
        ):
            self._paid_cost_failure("paid_test_cost_exceeds_reservation")
        if not {"prompt_tokens", "completion_tokens"} <= safe.keys():
            self._paid_cost_failure("usage_invalid")
