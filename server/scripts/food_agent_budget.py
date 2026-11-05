"""Fail-closed, pre-wire currency reservations for the opt-in live bench.

Amounts are upper bounds, not invoices. Reservations are NEVER released: even
missing usage, failures, timeouts and retries retain their complete allowance.
Text-only Terra Responses uses the consulted public rate. Hosted web search
requires a reviewed full-context/worst-rate card and an explicit one-tool limit.
Media and previous-response state are refused.
"""
from decimal import Decimal
import json
from collections.abc import Mapping

TERRA = "gpt-6.1-terra"
RATE_VERSION = "terra-standard-short-context-v1"
RATE_SOURCE = "https://developers.openai.com/api/docs/models/gpt-6.1-terra"


class PricingRefused(ValueError):
    pass


def money(value):
    try:
        number = Decimal(str(value))
    except Exception:
        raise PricingRefused("invalid rate") from None
    if not number.is_finite() or number < 0:
        raise PricingRefused("invalid rate")
    return number


class RateCard:
    """Explicit reviewed prices. Unknown provider charges are absent, never zero.

    Provider entries must be documented per-attempt upper bounds covering the
    account's contract/tier and all endpoints. A reviewed included/free plan may
    explicitly set zero. No account price is inferred from a key or subscription.
    """
    def __init__(self, *, version=RATE_VERSION, providers=None, evidence=None, hosted_models=None):
        self.version = version
        self.evidence = evidence or {}
        self.hosted_models = hosted_models or {}
        for model, rate in self.hosted_models.items():
            if not rate.get("source") or not rate.get("version"):
                raise PricingRefused("hosted model price evidence required")
            for field in ("input_per_million", "output_per_million", "tool_per_call_usd"):
                money(rate[field])
            if type(rate.get("max_context_tokens")) is not int or rate["max_context_tokens"] < 32000:
                raise PricingRefused("verified full context ceiling required")
        self.providers = {k: money(v) for k, v in (providers or {}).items()}
        if not isinstance(version, str) or not version.strip():
            raise PricingRefused("rate card version required")
        for key in self.providers:
            if key not in {"fatsecret", "exa", "cofid", "usda"} or not self.evidence.get(key):
                raise PricingRefused("provider price evidence required")

    @classmethod
    def load(cls, path):
        # Explicit nonsecret JSON rate card only; no dotenv or credential discovery.
        from pathlib import Path
        file = Path(path)
        if file.suffix != ".json":
            raise PricingRefused("rate card must be an explicit JSON file")
        data = json.loads(file.read_text())
        if set(data) not in ({"version", "providers", "evidence"}, {"version", "providers", "evidence", "hosted_models"}):
            raise PricingRefused("invalid rate card fields")
        return cls(**data)

    def lookup_cost(self, provider):
        if provider not in self.providers:
            raise PricingRefused("unpriced lookup route")
        return self.providers[provider]

    def model_cost(self, payload):
        if not isinstance(payload, Mapping):
            raise PricingRefused("unpriced model route")
        allowed = {"model", "input", "instructions", "text", "store", "reasoning", "max_output_tokens",
                   "service_tier", "tools", "max_tool_calls", "include"}
        if set(payload) - allowed:
            raise PricingRefused("unpriced request option")
        if payload.get("tools"):
            return self.hosted_cost(payload)
        if payload.get("model") != TERRA:
            raise PricingRefused("unpriced model route")
        if any(k in payload for k in
                ("previous_response_id", "conversation", "background", "file", "attachments")):
            raise PricingRefused("unpriced tool or stateful route")
        if payload.get("service_tier", "default") != "default":
            raise PricingRefused("unpriced service tier")
        # Validate text-only input. Remote media cannot be bounded by string length.
        inputs = payload.get("input")
        if isinstance(inputs, list):
            if not all(isinstance(x, Mapping) and set(x) <= {"role", "content"}
                       and isinstance(x.get("content"), str) for x in inputs):
                raise PricingRefused("unbounded input")
        elif not isinstance(inputs, str):
            raise PricingRefused("unbounded input")
        output = payload.get("max_output_tokens")
        if isinstance(output, bool) or not isinstance(output, int) or not 1 <= output <= 2000:
            raise PricingRefused("bounded output required")
        # UTF-8 byte count dominates text token count; include entire serialized
        # schema/instructions/envelope plus 4096 framing tokens. Cap below short
        # context pricing range; reject rather than guessing long-context prices.
        input_bound = len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 4096
        if input_bound > 32000:
            raise PricingRefused("short-context input reservation exceeded")
        cost = (Decimal(input_bound) * Decimal("3.85") + Decimal(output) * Decimal("5.50")) / Decimal(1000000)
        return cost, input_bound, output

    def hosted_cost(self, payload):
        rate = self.hosted_models.get(payload.get("model"))
        if not rate:
            raise PricingRefused("unpriced hosted model route")
        if payload.get("tools") != [{"type": "web_search"}] or payload.get("max_tool_calls") != 1:
            raise PricingRefused("unbounded hosted tool attempts")
        if not isinstance(payload.get("input"), str) or any(k in payload for k in
                ("previous_response_id", "conversation", "background")):
            raise PricingRefused("unbounded hosted input")
        if payload.get("service_tier", "default") != "default":
            raise PricingRefused("unpriced service tier")
        output = payload.get("max_output_tokens")
        if type(output) is not int or not 1 <= output <= 2000:
            raise PricingRefused("bounded hosted output required")
        # Reserve TWO full contexts (before and after the one tool) at the worst
        # applicable (including long
        # context) rate, plus output/reasoning cap and one paid hosted search.
        # Operator-supplied evidence must cover search-content token surcharges.
        limit = rate["max_context_tokens"]
        cost = (money(limit) * 2 * money(rate["input_per_million"]) +
                money(output) * money(rate["output_per_million"])) / 1000000
        cost += money(rate["tool_per_call_usd"])
        return cost, limit, output

    def manifest(self):
        return {"version": self.version, "model": TERRA, "model_price_version": RATE_VERSION,
                "model_source": RATE_SOURCE, "input_usd_per_million": "1", "output_usd_per_million": "5",
                "reservation_input_usd_per_million": "3.85", "reservation_output_usd_per_million": "5.50",
                "reservation_basis": "uncached input plus max 12h cache-write rate plus 10% regional premium; standard only",
                "provider_upper_bounds_usd": {k: str(v) for k, v in self.providers.items()},
                "provider_evidence": self.evidence, "hosted_models": self.hosted_models}
