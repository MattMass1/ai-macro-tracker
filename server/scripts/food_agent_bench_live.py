#!/usr/bin/env python3
"""Food agent bench, lane C: the LIVE model and LIVE food providers through the real canvas turn.

Lanes A/B prove the code does the right thing given a correct interpretation.
Lane C measures the real thing: Matthew's real user messages (the same turns
lanes A/B replay) go through ``CanvasService.turn`` with the food agent, and

* the interpreter is the production ``food_agent.model_adapter.ModelInterpreter``
  with ``FOOD_AGENT_MODEL`` explicitly set to ``gpt-6.1-terra`` and its
  production Responses transport wrapped by the shared count/currency meter,
* lookups are the production ``FoodFinder`` (``server.resolve_food``: presets,
  tenant catalog, CoFID, generic foods, FatSecret, Exa/web estimate) with the
  outbound provider functions wrapped by a counter in THIS process only,
* drafts are ``StoreDrafts`` on a throwaway local PostgreSQL (``--pg-admin-url``,
  refused unless it is local), history is seeded per run for fresh synthetic
  users, and the old canvas path is the bench's ``OldPath`` stub (any call
  fails the case).

Hard caps (US$10, ``--max-model-calls``, ``--max-lookups``) are checked BEFORE every
wire attempt, retries and failures included. A refused attempt stops the run;
the current and all remaining case x repeat runs are "not run (cap)", never a
failure, and the results file is still written.

Secrets: keys are read only from the process environment (``server/.env`` is
not loaded in lane C); nothing prints, logs or stores an env value, an auth
header, a provider body or ``str(exc)`` of a provider error. Logging is off.

stdout carries case ids, scores, caps and usage totals only. The results JSON
(private, outside the repo) holds transcripts and the per-item audit.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import food_agent_bench as bench  # noqa: E402  (also puts server/src on sys.path)

import httpx  # noqa: E402
from decimal import Decimal
from food_agent_budget import RateCard, PricingRefused, TERRA, money

MATTHEW_REAL_USER_ID = "be6333cc-e4c1-48f2-adb1-5e7f14dbf7c2"
DEFAULT_OUT = Path("/Users/matthewagent/.hermes/profiles/devops/reports/mmmacros-agent-build/bench/results-laneC.json")
LIVE_LABEL = "live"
NOT_RUN = "not run (cap)"
ZERO_TOLERANCE = ("false_logged", "duplicates", "stale_version_commits", "cross_tenant_leaks",
                  "committed_before_confirm")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_MODEL_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,100}$")
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


# ---------------------------------------------------------------- refusals


class CapReached(BaseException):
    """A hard cap stopped the run.

    A BaseException on purpose: production code treats a failed lookup as "not
    found" (``except Exception``), and a cap stop must never be swallowed into
    an ordinary outcome that would then be scored.
    """

    def __init__(self, kind: str, limit: int):
        super().__init__(f"{kind} cap reached ({limit})")
        self.kind, self.limit = kind, limit


class RealUserRefused(RuntimeError):
    """Lane C only ever runs as fresh synthetic users."""


def assert_synthetic_user(user_id: Any) -> None:
    try:
        value = UUID(str(user_id))
    except ValueError:
        raise RealUserRefused("lane C user ids must be synthetic uuids") from None
    from auth import DEFAULT_MATT_USER_ID
    if value in (UUID(MATTHEW_REAL_USER_ID), DEFAULT_MATT_USER_ID):
        raise RealUserRefused("refusing to run lane C as a real user")


def _host_is_local(host: str) -> bool:
    host = unquote(str(host or "")).strip()
    if host.startswith("/"):
        return True  # a unix socket directory
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host.casefold() in _LOCAL_HOSTS


def _netloc_hosts(netloc: str) -> list[str] | None:
    hosts = []
    for chunk in netloc.rpartition("@")[2].split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.startswith("["):
            end = chunk.find("]")
            if end < 0:
                return None
            hosts.append(chunk[:end + 1])
        else:
            hosts.append(chunk.rsplit(":", 1)[0] if chunk.count(":") == 1 else chunk)
    return hosts


def is_local_pg_url(url: Any, *, env: Mapping[str, str] | None = None) -> bool:
    """Whether a PostgreSQL URL can only reach this machine.

    Local means every host is localhost, 127.0.0.1, ::1 or a unix socket path
    (an empty host, a ``host=/path`` query, or a host starting with /). An
    empty host falls back to ``PGHOST``, which must then be local too. Pure:
    no DNS, no connection.
    """
    env = os.environ if env is None else env
    if env.get("PGSERVICE") or env.get("PGSERVICEFILE"):
        return False
    if env.get("PGHOSTADDR") and not all(_host_is_local(h) for h in env["PGHOSTADDR"].split(",")):
        return False
    text = str(url or "").strip()
    if not text:
        return False
    if text.startswith("/"):
        return True
    try:
        parts = urlsplit(text)
        query = parse_qs(parts.query, keep_blank_values=True)
    except ValueError:
        return False
    if any(key in query for key in ("service", "servicefile", "passfile")):
        return False
    if parts.scheme.casefold() not in ("postgres", "postgresql"):
        return False
    hosts = _netloc_hosts(parts.netloc)
    if hosts is None:
        return False
    for key in ("host", "hostaddr"):
        for value in query.get(key, []):
            for host in value.split(","):
                if (key == "hostaddr" and host.strip().startswith("/")) or not _host_is_local(host):
                    return False
    if hosts:
        return all(_host_is_local(h) for h in hosts)
    if query.get("host"):
        return True
    pghost = str(env.get("PGHOST") or "").strip()
    return not pghost or all(_host_is_local(h) for h in pghost.split(","))


def worktree_root(start: Path | None = None) -> Path | None:
    here = (start or Path(__file__)).resolve()
    for folder in (here, *here.parents):
        if (folder / ".git").exists():
            return folder
    return None


def out_inside_worktree(out: Any) -> bool:
    root = worktree_root()
    if root is None:
        return False
    target = Path(out).expanduser().resolve()
    return target == root or root in target.parents


def preflight(*, pg_admin_url: str, out: Any, env: Mapping[str, str] | None = None, rate_card=None) -> list[str]:
    """Every reason lane C must not start. Runs before any connection or network call; never echoes a value."""
    env = os.environ if env is None else env
    errors = []
    if not pg_admin_url:
        errors.append("lane C needs --pg-admin-url (a throwaway local PostgreSQL)")
    elif not is_local_pg_url(pg_admin_url, env=env):
        errors.append("--pg-admin-url is not local")
    database_url = str(env.get("DATABASE_URL") or "").strip()
    if database_url and not is_local_pg_url(database_url, env=env):
        errors.append("DATABASE_URL is not local")
    if out_inside_worktree(out):
        errors.append("--out is inside the git worktree")
    errors.extend(route_errors(env, rate_card))
    return errors


def route_manifest(env):
    """Nonsecret configured routes. Never echo any credential value."""
    return {"interpreter": {"endpoint": "responses", "model": env.get("FOOD_AGENT_MODEL") or TERRA},
            "extraction": {"endpoint": "responses", "model": env.get("WEB_LOOKUP_EXTRACT_MODEL")},
            "fallback": {"endpoint": "responses/web_search", "model": env.get("COACH_MODEL")},
            "food": "FatSecret", "web": env.get("WEB_LOOKUP_PROVIDER")}


def route_errors(env, rate_card=None):
    errors = []
    if not (env.get("OPENAI_API_KEY", "").strip() or env.get("OPENAI_ACCESS_TOKEN", "").strip()):
        errors.append("backend OPENAI_API_KEY (or legacy OPENAI_ACCESS_TOKEN) required")
    if env.get("FOOD_AGENT_MODEL") != TERRA:
        errors.append("explicit approved FOOD_AGENT_MODEL required")
    if env.get("FATSECRET_ATTRIBUTION_ENABLED", "").casefold() != "true":
        errors.append("FatSecret attribution permission gate required")
    if not ((env.get("FATSECRET_CLIENT_ID") and env.get("FATSECRET_CLIENT_SECRET")) or
            (env.get("FATSECRET_CONSUMER_KEY") and env.get("FATSECRET_CONSUMER_SECRET"))):
        errors.append("FatSecret backend credentials required")
    if env.get("WEB_LOOKUP_PROVIDER") not in ("exa", "openai"):
        errors.append("explicit WEB_LOOKUP_PROVIDER required")
    if not env.get("OPENAI_ACCESS_TOKEN", "").strip():
        errors.append("existing web lookup OPENAI_ACCESS_TOKEN required")
    if env.get("WEB_LOOKUP_PROVIDER") == "exa":
        if not env.get("EXA_API_KEY", "").strip():
            errors.append("configured Exa route credential required")
        if env.get("WEB_LOOKUP_EXTRACT_MODEL") != TERRA:
            errors.append("explicit priced WEB_LOOKUP_EXTRACT_MODEL required")
    if not env.get("COACH_MODEL"):
        errors.append("explicit fallback COACH_MODEL required (not changed by harness)")
    if rate_card is None:
        errors.append("reviewed versioned route rate card required")
    else:
        needed = {"fatsecret", "cofid"} | ({"exa"} if env.get("WEB_LOOKUP_PROVIDER") == "exa" else set())
        if not needed <= rate_card.providers.keys():
            errors.append("unpriced food lookup routes")
        if env.get("COACH_MODEL") not in rate_card.hosted_models:
            errors.append("unpriced hosted fallback route")
    return errors


def safe_error(exc: BaseException) -> str:
    """An exception class plus a short safe code. Never ``str(exc)``: httpx messages can carry URLs with keys."""
    name = type(exc).__name__
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{name}: http_{exc.response.status_code}"
    reason = getattr(exc, "reason", None)  # NutritionLookupError carries a fixed reason code
    if isinstance(reason, str) and _SAFE_REASON.match(reason):
        return f"{name}: {reason}"
    return name


# ---------------------------------------------------------------- the budget (caps and records)


class Budget:
    """Counts every wire attempt and refuses one BEFORE it is made once a cap would be exceeded."""

    def __init__(self, max_model_calls: int = 300, max_lookups: int = 200, *, max_usd=10, rate_card=None):
        if type(max_model_calls) is not int or not 0 <= max_model_calls <= 300 or type(max_lookups) is not int or not 0 <= max_lookups <= 200:
            raise ValueError("authorized count ceilings are 300 model attempts and 200 lookups")
        self.max_model_calls, self.max_lookups = max_model_calls, max_lookups
        self.max_usd = money(max_usd)
        if self.max_usd > 10:
            raise ValueError("authorized currency ceiling is US$10")
        self.rate_card = rate_card or RateCard()
        self.reserved_usd = Decimal(0)
        self.model_calls: list[dict[str, Any]] = []
        self.lookup_attempts: list[dict[str, Any]] = []
        self.resolver_calls: list[dict[str, Any]] = []
        self.refused = {"model": 0, "lookup": 0}
        self.tripped: str | None = None
        self.where: dict[str, Any] = {"case_id": None, "repeat": None, "turn": None}

    @property
    def model_calls_used(self) -> int:
        return len(self.model_calls)

    @property
    def lookups_used(self) -> int:
        return len(self.lookup_attempts)

    def _check(self, kind: str, used: int, limit: int) -> None:
        if self.tripped is None and used < limit:
            return
        self.refused[kind] += 1
        self.tripped = self.tripped or kind
        raise CapReached(self.tripped, self.max_model_calls if self.tripped == "model" else self.max_lookups)

    def _reserve_cost(self, cost):
        if self.reserved_usd + cost > self.max_usd:
            self.tripped = "dollars"
            raise CapReached("dollars", self.max_usd)
        self.reserved_usd += cost

    def _unpriced(self):
        self.tripped = "unpriced"
        raise CapReached("unpriced", 0)

    def reserve_model(self, payload) -> dict[str, Any]:
        self._check("model", self.model_calls_used, self.max_model_calls)
        try:
            cost, input_bound, output_bound = self.rate_card.model_cost(payload)
        except PricingRefused:
            self._unpriced()
        self._reserve_cost(cost)
        record = {"seq": self.model_calls_used + 1, **self.where, "requested_model": None, "status": None,
                  "http_status": None, "latency_ms": None, "model": None, "prompt_tokens": None,
                  "completion_tokens": None, "total_tokens": None, "error": None}
        record.update(reserved_usd=str(cost), input_token_bound=input_bound, output_token_bound=output_bound,
                      rate_version=self.rate_card.version)
        self.model_calls.append(record)
        return record

    def reserve_lookup(self, provider: str, *, model_extraction=False) -> dict[str, Any]:
        self._check("lookup", self.lookups_used, self.max_lookups)
        if not model_extraction:
            try:
                cost = self.rate_card.lookup_cost(provider)
            except PricingRefused:
                self._unpriced()
            self._reserve_cost(cost)
        record = {"seq": self.lookups_used + 1, **self.where, "provider": provider, "status": None,
                  "http_status": None, "latency_ms": None, "error": None}
        self.lookup_attempts.append(record)
        return record


def usage_fields(data: Any) -> dict[str, Any]:
    """The provider's model id and token usage. Missing is None, never 0."""
    out: dict[str, Any] = {"model": None, "prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
    if not isinstance(data, Mapping):
        return out
    model = data.get("model")
    if isinstance(model, str) and _MODEL_ID.match(model):
        out["model"] = model
    usage = data.get("usage")
    if isinstance(usage, Mapping):
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key, usage.get({"prompt_tokens": "input_tokens", "completion_tokens": "output_tokens"}.get(key, key)))
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                out[key] = value
    return out


async def _timed(record: dict[str, Any], call) -> Any:
    t0 = time.perf_counter()
    try:
        result = await call()
    except (httpx.TimeoutException, TimeoutError) as exc:
        record.update(status="timeout", error=safe_error(exc))
        raise
    except asyncio.CancelledError:
        record.update(status="cancelled")
        raise
    except Exception as exc:
        record.update(status="error", error=safe_error(exc))
        raise
    finally:
        record["latency_ms"] = round((time.perf_counter() - t0) * 1000, 3)
    if isinstance(result, httpx.Response):
        record["http_status"] = result.status_code
        ok = 200 <= result.status_code < 300
        record["status"] = "ok" if ok else "error"
        if not ok:
            record["error"] = f"http_{result.status_code}"
    else:
        record["status"] = "ok"
    return result


def counted_model_post(real_post, budget: Budget):
    """The interpreter's ``post``: one counted, capped wire attempt per call (the adapter itself never retries)."""

    async def post(token: str, payload: dict[str, Any]) -> httpx.Response:
        record = budget.reserve_model(payload)
        requested = payload.get("model") if isinstance(payload, Mapping) else None
        record["requested_model"] = requested if isinstance(requested, str) and _MODEL_ID.match(requested) else None
        response = await _timed(record, lambda: real_post(token, payload))
        if record["status"] == "ok":  # an error body is never read
            try:
                data = response.json()
            except Exception:  # noqa: BLE001 - unreadable body: usage unknown
                data = None
            record.update(usage_fields(data))
        return response

    return post


def counted_post(real_post, budget: Budget, provider: str):
    """Exa search / OpenAI Responses (web estimate): exactly one HTTP request per call."""

    async def post(key: str, payload: dict[str, Any]) -> httpx.Response:
        record = budget.reserve_lookup(provider)
        return await _timed(record, lambda: real_post(key, payload))

    return post


def counted_fetch(real_fetch, budget: Budget, provider: str = "cofid"):
    async def fetch():
        record = budget.reserve_lookup(provider)
        return await _timed(record, real_fetch)

    return fetch


def _provider_for(url: Any) -> str:
    try:
        host = httpx.URL(str(url)).host.casefold()
    except Exception:  # noqa: BLE001
        return "other"
    if "fatsecret" in host:
        return "fatsecret"
    if "openfoodfacts" in host:
        return "openfoodfacts"
    return "other"


def counted_client(make_client, budget: Budget):
    """``food_lookup._client`` replacement: the production client whose ``request`` is counted.

    ``food_lookup._request_with_backoff`` calls ``client.request`` once per attempt (retries included), and the
    FatSecret token fetch goes through the same client, so this sees every FatSecret wire attempt while keeping
    the production client (timeouts, headers, proxies) as is.
    """

    def make():
        client = make_client()
        real_request = client.request

        async def request(method, url, *args, **kwargs):
            record = budget.reserve_lookup(_provider_for(url))
            return await _timed(record, lambda: real_request(method, url, *args, **kwargs))

        client.request = request
        return client

    return make


@contextlib.contextmanager
def install_counters(budget: Budget, *, exa_post=None, web_post=None, cofid_fetch=None, usda_post=None):
    """Wrap the outbound provider functions, in this process only, for the duration of the run."""
    import cofid_lookup
    import food_lookup
    import usda_lookup
    import web_nutrition_lookup as web

    saved = [(food_lookup, "_client", food_lookup._client), (web, "post_exa_search", web.post_exa_search),
             (web, "post_responses", web.post_responses), (cofid_lookup, "_fetch_workbook", cofid_lookup._fetch_workbook),
             (usda_lookup, "post_search", usda_lookup.post_search)]
    food_lookup._client = counted_client(food_lookup._client, budget)
    real_usda = usda_post or usda_lookup.post_search

    async def counted_usda(params):
        record = budget.reserve_lookup("usda")
        return await _timed(record, lambda: real_usda(params))
    usda_lookup.post_search = counted_usda
    usda_lookup.clear_cache()
    web.post_exa_search = counted_post(exa_post or web.post_exa_search, budget, "exa")
    real_web = counted_model_post(web_post or web.post_responses, budget)
    async def counted_web(key, payload):
        # Model extraction is both a lookup and a model attempt; hosted fallback
        # gets an explicit one-tool-call limit before reservation and wire.
        payload = dict(payload)
        if payload.get("tools"):
            payload["max_tool_calls"] = 1
        budget._check("model", budget.model_calls_used, budget.max_model_calls)
        budget.reserve_lookup("web", model_extraction=True)
        return await real_web(key, payload)
    web.post_responses = counted_web
    cofid_lookup._fetch_workbook = counted_fetch(cofid_fetch or cofid_lookup._fetch_workbook, budget, "cofid")
    try:
        yield
    finally:
        for module, name, value in saved:
            setattr(module, name, value)


def build_interpreter(budget: Budget, model_post=None):
    """The production ``ModelInterpreter`` (model, endpoint and token chosen exactly as in production); only its
    ``post`` is wrapped by the counter. ``model_post`` replaces the real Responses transport in tests."""
    from food_agent.model_adapter import ModelInterpreter
    if model_post is None:
        from food_agent.model_adapter import post_responses
        model_post = post_responses
    if bench.agent2():
        from food_agent2.interpret import ModelInterpreter as Agent2Interpreter
        return Agent2Interpreter(post=counted_model_post(model_post, budget))
    return ModelInterpreter(post=counted_model_post(model_post, budget))


# ---------------------------------------------------------------- per-run plumbing


class LiveInterpreter:
    """Gives ``run_case`` the ``index``/``sent``/``unexpected`` it reads; the model itself is untouched."""

    def __init__(self, inner):
        self.inner = inner
        self.index: int | None = None
        self.unexpected = 0
        self.sent: list[list[str]] = []

    async def __call__(self, message, context):
        turn = await self.inner(message, context)
        self.sent.append([action.type for action in turn.actions])
        return turn


def _found_provider(found: Mapping[str, Any]) -> str:
    attribution = found.get("attribution") if isinstance(found.get("attribution"), Mapping) else {}
    provider = str(attribution.get("provider") or "").casefold()
    if provider.startswith("fatsecret") or str(found.get("source") or "").startswith("FatSecret:"):
        return "fatsecret"
    if "exa" in provider:
        return "exa"
    if found.get("estimate_provenance") == "server_web_estimate" or "web" in provider:
        return "web"
    return "local"


class ResolverLog:
    """Wraps the production resolver and suggester: provider and hit/miss/error per lookup, plus the provider's
    brand per resolved food id (the saved item keeps only the display name)."""

    def __init__(self, budget: Budget):
        self.budget = budget
        self.brands: dict[str, str] = {}

    def _record(self, kind: str) -> tuple[dict[str, Any], int]:
        record = {**self.budget.where, "kind": kind, "provider": None, "result": None, "error": None,
                  "providers_tried": [], "latency_ms": None}
        self.budget.resolver_calls.append(record)
        return record, self.budget.lookups_used

    def _close(self, record, before, t0):
        record["latency_ms"] = round((time.perf_counter() - t0) * 1000, 3)
        tried = [a["provider"] for a in self.budget.lookup_attempts[before:]]
        record["providers_tried"] = sorted(set(tried))
        record["provider_attempts"] = len(tried)

    def wrap_resolve(self, real):
        async def resolve(query):
            record, before = self._record("resolve")
            t0 = time.perf_counter()
            try:
                found = await real(query)
            except Exception as exc:
                record.update(result="error", error=safe_error(exc))
                raise
            finally:
                self._close(record, before, t0)
            if isinstance(found, Mapping):
                record.update(result="hit", provider=_found_provider(found))
                self._remember(found)
            else:
                record.update(result="miss", provider="none")
            return found
        return resolve

    def wrap_suggest(self, real):
        async def suggest(query):
            record, before = self._record("suggest")
            t0 = time.perf_counter()
            try:
                names = await real(query)
            except Exception as exc:
                record.update(result="error", error=safe_error(exc))
                raise
            finally:
                self._close(record, before, t0)
            record.update(result="hit" if names else "miss", provider="fatsecret", options=len(names or []))
            return names
        return suggest

    def _remember(self, found: Mapping[str, Any]) -> None:
        from food_agent.find_food import to_resolved_food
        try:
            food = to_resolved_food(found)
        except Exception:  # noqa: BLE001 - the finder will judge it; the audit just lacks a brand
            return
        if food is not None:
            self.brands[food.food_id] = str(found.get("brand_name") or "").strip()


class LiveHooks:
    def __init__(self, budget: Budget, case_id: str, repeat: int):
        self.budget, self.case_id, self.repeat = budget, case_id, repeat
        self.turns: list[dict[str, Any]] = []
        self._marks = (0, 0)

    def case_ready(self, env) -> None:
        assert_synthetic_user(env.user_a)
        assert_synthetic_user(env.user_b)

    def turn_start(self, index: int) -> None:
        self.budget.where = {"case_id": self.case_id, "repeat": self.repeat, "turn": index}
        self._marks = (self.budget.model_calls_used, self.budget.lookups_used)

    def turn_end(self, record) -> None:
        self.turns.append({"idx": record.idx, "elapsed_ms": round(record.elapsed_ms, 3), "error": record.error,
                           "synthetic": record.synthetic,
                           "model_calls": self.budget.model_calls_used - self._marks[0],
                           "provider_attempts": self.budget.lookups_used - self._marks[1]})
        if self.budget.tripped:  # a refused attempt was swallowed somewhere: the run still stops here
            raise CapReached(self.budget.tripped, 0)


# ---------------------------------------------------------------- scoring


def branded_name(display_name: Any, brand: Any) -> str:
    """The resolved food as the scorer reads it: the provider brand + display name, never doubling the brand."""
    name, brand = str(display_name or ""), str(brand or "").strip()
    if not brand or set(bench._tokens(brand)) <= set(bench._tokens(name)):
        return name
    return f"{brand} {name}"


def score_live(case: dict[str, Any], script: dict[str, Any], run, brands: Mapping[str, str]) -> dict[str, Any]:
    """The lanes A/B scorer (identity guards, gold multiplier, zero-tolerance counters) on a live run.

    Live lookups return provider ids, so identity is the resolved display name plus the provider's brand,
    matched against the gold identity (brand/place + item + variant). Every saved item is listed raw for audit.
    """
    scoring = copy.deepcopy(run)
    gold = [g["identity"] for g in case.get("gold_items") or []]
    audit = []
    for entry in scoring.entries:
        for item in (entry.get("component_metadata") or {}).get("items") or []:
            raw = item.get("display_name")
            brand = brands.get(str(item.get("food_id"))) or ""
            scored_name = branded_name(raw, brand)
            item["display_name"] = scored_name
            audit.append({"entry_id": entry.get("id"), "raw_display_name": raw, "brand": brand or None,
                          "scored_name": scored_name, "provider": item.get("provider"),
                          "external_id": item.get("external_id"), "food_id": item.get("food_id"),
                          "macro_source": item.get("macro_source"), "multiplier": item.get("multiplier"),
                          "serving_basis": item.get("serving_basis"),
                          "matches_gold": [i for i, g in enumerate(gold) if bench.identity_matches(g, scored_name)]})
    scored = bench.score_case(case, script, scoring, live_basis=True)
    scored["items_audit"] = audit
    return scored


# ---------------------------------------------------------------- running


async def run_live_bench(spec: dict[str, Any], backend, budget: Budget, *, repeats: int = 3, model_post=None,
                         exa_post=None, web_post=None, cofid_fetch=None, local_store=None,
                         usda_post=None) -> dict[str, Any]:
    """Every case x repeat (repeat-major, so a cap still leaves one pass over the cases), stopping at a cap."""
    errors = route_errors(os.environ, budget.rate_card)
    if getattr(backend, "url", None) and not is_local_pg_url(backend.url):
        errors.append("backend database is not local")
    if errors:
        runs = [{"case_id": case["id"], "repeat": repeat, "status": "not run (configuration)",
                 "configuration_errors": errors} for repeat in range(1, repeats + 1) for case in spec["cases"]]
        result = build_results(spec, runs, budget, backend, repeats, None)
        result["configuration_errors"] = errors
        return result
    # server reads its config at import; its store is replaced below, so this URL is never the one used for
    # food data. The CLI has already pointed it at the throwaway database; an unset value gets the same.
    os.environ.setdefault("DATABASE_URL", getattr(backend, "url", None) or "postgresql://fixture.invalid/lane_c")
    os.environ.setdefault("APP_SHARED_TOKEN", "bench-shared-token")
    import config
    config._ENV_LOADED = True
    import server
    from food_agent.find_food import FoodFinder, production_resolve, production_suggest

    local_store = local_store if local_store is not None else getattr(backend, "store", None)
    if local_store is None:
        raise ValueError("lane C needs a store for the local food data (presets, catalog)")
    plan = [(repeat, case) for repeat in range(1, repeats + 1) for case in spec["cases"]]
    runs: list[dict[str, Any]] = []
    stopped: str | None = None
    saved_client = server._client
    server._client = local_store  # server.resolve_food reads presets/catalog from the throwaway store
    try:
        with install_counters(budget, exa_post=exa_post, web_post=web_post, cofid_fetch=cofid_fetch,
                              usda_post=usda_post):
            model = build_interpreter(budget, model_post)
            for repeat, case in plan:
                entry: dict[str, Any] = {"case_id": case["id"], "repeat": repeat}
                if stopped:
                    entry.update(status=NOT_RUN, interrupted=False)
                    runs.append(entry)
                    continue
                script = spec["scripts"][case["id"]]
                log, hooks = ResolverLog(budget), LiveHooks(budget, case["id"], repeat)
                marks = (budget.model_calls_used, budget.lookups_used, len(budget.resolver_calls))
                budget.where = {"case_id": case["id"], "repeat": repeat, "turn": None}

                def parts(_case, _script, _env, log=log):
                    if bench.agent2():
                        from food_agent2.sources import LiveSources
                        return LiveInterpreter(model), LiveSources()
                    finder = FoodFinder(log.wrap_resolve(production_resolve),
                                        suggest=log.wrap_suggest(production_suggest))
                    return LiveInterpreter(model), finder

                run = None
                try:
                    run = await bench.run_case(case, script, backend, parts=parts, errors=safe_error, hooks=hooks)
                except CapReached:
                    pass
                entry.update(model_calls=budget.model_calls[marks[0]:],
                             provider_attempts=budget.lookup_attempts[marks[1]:],
                             resolver_calls=budget.resolver_calls[marks[2]:], turns_live=hooks.turns)
                if run is None or budget.tripped:
                    stopped = budget.tripped or "unknown"
                    entry.update(status=NOT_RUN, interrupted=True)
                    runs.append(entry)
                    continue
                scored = score_live(case, script, run, log.brands)
                infra = any(r.get("result") == "error" for r in entry["resolver_calls"])
                infra = infra or any(r.get("status") in ("error", "timeout", "cancelled") for r in entry["model_calls"])
                if infra and case.get("expected_action") in bench.CLARIFY_ACTIONS:
                    scored["success"] = scored["success_strict"] = False
                    scored["reasons"].append("infrastructure error is not successful clarification")
                live_turns = {t["idx"]: t for t in hooks.turns}
                entry.update(status="run", items_audit=scored.pop("items_audit"), score=scored, transcript=[
                    {"user": t.user, "synthetic": t.synthetic, "reply": t.reply, "question": t.is_question,
                     "status": t.status, "model": t.model_types, "rows_after": t.entries_after, "error": t.error,
                     "elapsed_ms": round(t.elapsed_ms, 3),
                     "model_calls": live_turns.get(t.idx, {}).get("model_calls"),
                     "provider_attempts": live_turns.get(t.idx, {}).get("provider_attempts")} for t in run.turns])
                runs.append(entry)
    finally:
        server._client = saved_client
        budget.where = {"case_id": None, "repeat": None, "turn": None}
    return build_results(spec, runs, budget, backend, repeats, stopped)


def token_totals(calls: list[dict[str, Any]]) -> dict[str, Any]:
    def total(key):
        values = [c[key] for c in calls if c.get(key) is not None]
        return sum(values) if values else None
    known = sum(1 for c in calls if c.get("total_tokens") is not None)
    return {"prompt_tokens": total("prompt_tokens"), "completion_tokens": total("completion_tokens"),
            "total_tokens": total("total_tokens"), "tokens_known_calls": known,
            "tokens_unknown_calls": len(calls) - known}


def _pcts(values: list[float]) -> dict[str, Any]:
    return {"n": len(values), "p50": bench._r(bench.percentile(values, 50)),
            "p95": bench._r(bench.percentile(values, 95))}


def build_results(spec, runs, budget: Budget, backend, repeats: int, stopped: str | None) -> dict[str, Any]:
    cases = spec["cases"]
    done = [r for r in runs if r["status"] == "run"]
    scores = [r["score"] for r in done]
    agent = bench.summarize(scores, "C", getattr(backend, "name", "?")) if scores else None
    planned = len(runs)

    per_case = {}
    for case in cases:
        mine = [r for r in runs if r["case_id"] == case["id"]]
        ran = [r for r in mine if r["status"] == "run"]
        strict = sum(1 for r in ran if r["score"]["success_strict"])
        with_yes = sum(1 for r in ran if r["score"]["success"])
        per_case[case["id"]] = {
            "strict": f"{strict}/{len(mine)}", "with_yes": f"{with_yes}/{len(mine)}",
            "strict_n": strict, "with_yes_n": with_yes, "planned": len(mine), "completed": len(ran),
            "not_run_cap": sum(r["status"] == NOT_RUN for r in mine),
            "not_run_configuration": sum(r["status"] == "not run (configuration)" for r in mine),
            "questions_per_run": [r["score"]["questions"]["total"] for r in ran],
            "old_app_questions": int((case.get("old_app") or {}).get("questions_asked") or 0),
            "old_app_success": bool((case.get("old_app") or {}).get("case_success")),
            "label_conflict": (spec["scripts"][case["id"]].get("scoring") or {}).get("label_conflict")}

    old = [c.get("old_app") or {} for c in cases]
    classes = {k: {"agent_runs": sum(s["failure_classes"][k] for s in scores), "completed_runs": len(scores),
                   "old_app_cases": sum(1 for o in old if o.get(k)), "cases": len(cases)}
               for k in ("wrong_past_meal", "lost_fraction", "reasked_confirmed_item")}
    turn_ms = [t["elapsed_ms"] for r in runs for t in r.get("turns_live") or []]
    call_ms = [c["latency_ms"] for c in budget.model_calls if c.get("latency_ms") is not None]
    by_provider: dict[str, dict[str, int]] = {}
    for attempt in budget.lookup_attempts:
        by_provider.setdefault(attempt["provider"], Counter())[str(attempt["status"])] += 1
    resolutions: dict[str, dict[str, int]] = {}
    for call in budget.resolver_calls:
        resolutions.setdefault(f"{call['kind']}:{call['provider']}", Counter())[str(call["result"])] += 1

    summary = {
        "lane": "C", "label": LIVE_LABEL, "drafts_backend": getattr(backend, "name", "?"), "repeats": repeats,
        "runs": {"planned": planned, "completed": len(done),
                 "not_run_cap": sum(r["status"] == NOT_RUN for r in runs),
                 "not_run_configuration": sum(r["status"] == "not run (configuration)" for r in runs)},
        "case_success": bench._counted(sum(1 for s in scores if s["success"]), planned),
        "case_success_strict": bench._counted(sum(1 for s in scores if s["success_strict"]), planned),
        "case_success_behavior": bench._counted(sum(1 for s in scores if s.get("success_behavior")), planned),
        "case_success_of_completed": bench._counted(sum(1 for s in scores if s["success"]), len(scores)),
        "case_success_strict_of_completed": bench._counted(sum(1 for s in scores if s["success_strict"]),
                                                           len(scores)),
        "stopped_by_cap": stopped,
        "caps": {"max_model_calls": budget.max_model_calls, "max_lookups": budget.max_lookups,
                 "model_calls_used": budget.model_calls_used, "lookups_used": budget.lookups_used,
                 "refused": dict(budget.refused)},
        "currency": {"ceiling_usd": str(budget.max_usd), "reserved_upper_bound_usd": str(budget.reserved_usd),
                     "billed_spend_usd": None, "reservations_released": False},
        "model_calls": budget.model_calls_used,
        "model_call_status": dict(Counter(str(c["status"]) for c in budget.model_calls)),
        "models_requested": sorted({c["requested_model"] for c in budget.model_calls if c["requested_model"]}),
        "models_returned": sorted({c["model"] for c in budget.model_calls if c["model"]}),
        "lookups": budget.lookups_used,
        "lookup_attempts_by_provider": {k: dict(v) for k, v in sorted(by_provider.items())},
        "resolver_calls": {k: dict(v) for k, v in sorted(resolutions.items())},
        "tokens": token_totals(budget.model_calls),
        "latency_ms": {"label": LIVE_LABEL, "turn": _pcts(turn_ms), "model_call": _pcts(call_ms)},
        "zero_tolerance": agent["zero_tolerance"] if agent else {k: 0 for k in ZERO_TOLERANCE},
        "identity": agent["identity"] if agent else bench._counted(0, 0),
        "portion": agent["portion"] if agent else bench._counted(0, 0),
        "user_numbers": agent["user_numbers"] if agent else bench._counted(0, 0),
        "failure_classes": classes,
        "questions": {"agent_total": sum(s["questions"]["total"] for s in scores),
                      "agent_per_run": round(sum(s["questions"]["total"] for s in scores) / max(1, len(scores)), 3),
                      "old_app_total": sum(int(o.get("questions_asked") or 0) for o in old),
                      "old_app_per_case": round(sum(int(o.get("questions_asked") or 0) for o in old)
                                                / max(1, len(cases)), 3)},
        "old_app": {"case_success": bench._counted(sum(1 for o in old if o.get("case_success")), len(cases))},
        "old_path_calls": sum(s["old_path_calls"] for s in scores),
        "synthetic_confirms": sum(s["synthetic_confirms"] for s in scores),
    }
    return {"version": 1, "lane": "C", "label": LIVE_LABEL, "source": spec.get("source"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "config": {"routes": route_manifest(os.environ), "rate_card": budget.rate_card.manifest(),
                       "repeats": repeats, "max_model_calls": budget.max_model_calls,
                       "max_lookups": budget.max_lookups,
                       "confirm_rules": "lanes A/B: synthetic final yes after an unconfirmed preview "
                                        "(and a script's confirm_before); strict excludes any synthetic turn",
                       "order": "repeat-major"},
            "runs": runs, "per_case": per_case, "summary": summary}


# ---------------------------------------------------------------- output


def print_summary(results: dict[str, Any], out: Path | None = None, file=None) -> None:
    """Case ids and numbers only: no message text, replies or food names."""
    file = file or sys.stdout
    s = results["summary"]

    def p(text=""):
        print(text, file=file)

    p("food agent bench lane C (live)")
    if out is not None:
        p(f"results written: {out}")
    for case_id, row in results["per_case"].items():
        p(f"  {case_id}: strict {row['strict']}  with-yes {row['with_yes']}  {NOT_RUN} {row['not_run_cap']}")
    runs = s["runs"]
    p(f"overall: strict {s['case_success_strict']['n']}/{s['case_success_strict']['d']}  "
      f"with-yes {s['case_success']['n']}/{s['case_success']['d']}  (over all case x repeat runs planned; "
      f"completed {runs['completed']}, {NOT_RUN} {runs['not_run_cap']})")
    caps = s["caps"]
    p(f"caps: model calls {caps['model_calls_used']}/{caps['max_model_calls']}  provider lookups "
      f"{caps['lookups_used']}/{caps['max_lookups']}  refused model {caps['refused']['model']} lookup "
      f"{caps['refused']['lookup']}  stopped by cap: {s['stopped_by_cap'] or 'none'}")
    status = ", ".join(f"{k} {v}" for k, v in sorted(s["model_call_status"].items())) or "none"
    providers = ", ".join(f"{k} {sum(v.values())}" for k, v in s["lookup_attempts_by_provider"].items()) or "none"
    t = s["tokens"]
    p(f"usage: model calls {s['model_calls']} ({status}); provider attempts {s['lookups']} ({providers}); tokens "
      f"prompt {t['prompt_tokens']} completion {t['completion_tokens']} total {t['total_tokens']} "
      f"(known calls {t['tokens_known_calls']}, unknown calls {t['tokens_unknown_calls']})")
    lat = s["latency_ms"]
    p(f"latency ({lat['label']}): turn p50 {lat['turn']['p50']} ms p95 {lat['turn']['p95']} ms (n {lat['turn']['n']})"
      f"; model call p50 {lat['model_call']['p50']} ms p95 {lat['model_call']['p95']} ms")
    p("zero tolerance: " + ", ".join(f"{k} {v}" for k, v in s["zero_tolerance"].items()))


def write_results(path: Path, results: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=2, default=str))


async def execute(spec, backend, budget: Budget, *, out: Path, repeats: int = 3, stdout=None,
                  **providers) -> int:
    """Run, then ALWAYS write the results (a cap stop included) and print the id-and-number summary."""
    try:
        results = await run_live_bench(spec, backend, budget, repeats=repeats, **providers)
    except BaseException as exc:
        write_results(out, {"version": 1, "lane": "C", "label": LIVE_LABEL, "aborted": safe_error(exc),
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                            "summary": {"caps": {"max_model_calls": budget.max_model_calls,
                                                 "max_lookups": budget.max_lookups,
                                                 "model_calls_used": budget.model_calls_used,
                                                 "lookups_used": budget.lookups_used,
                                                 "refused": dict(budget.refused)},
                                        "tokens": token_totals(budget.model_calls)},
                            "model_calls": budget.model_calls, "provider_attempts": budget.lookup_attempts})
        print(f"lane C aborted: {safe_error(exc)}", file=sys.stderr)
        raise
    write_results(out, results)
    print_summary(results, out, file=stdout)
    return 2 if results.get("configuration_errors") else 0


def main_live(args) -> int:
    """CLI entry (``food_agent_bench.py --lane C``). Refusals happen before any connection or network call."""
    out = Path(args.out) if args.out else DEFAULT_OUT
    rate_card = None
    try:
        if args.rate_card:
            rate_card = RateCard.load(args.rate_card)
    except (ValueError, OSError, KeyError, TypeError):
        print("lane C refused: invalid rate card", file=sys.stderr)
        return 2
    errors = preflight(pg_admin_url=args.pg_admin_url, out=out, env=os.environ, rate_card=rate_card)
    if args.repeats < 1 or not 0 <= args.max_model_calls <= 300 or not 0 <= args.max_lookups <= 200:
        errors.append("--repeats must be at least 1 and the caps not negative")
    if errors:
        print("lane C refused: " + "; ".join(errors), file=sys.stderr)
        return 2
    spec = bench.load_bench(args.bench, args.scripts)
    logging.disable(logging.CRITICAL)  # provider errors are logged with exception text elsewhere; none here
    import config
    config._ENV_LOADED = True  # keys come only from the process environment, never a .env file

    async def go() -> int:
        before = os.environ.get("DATABASE_URL")
        backend = await bench.PgBackend.create(args.pg_admin_url)
        os.environ["DATABASE_URL"] = backend.url  # anything that reads it reaches only the throwaway database
        try:
            return await execute(spec, backend, Budget(args.max_model_calls, args.max_lookups, rate_card=rate_card), out=out,
                                 repeats=args.repeats)
        finally:
            await backend.aclose()
            if before is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = before

    return asyncio.run(go())


__all__ = ["Budget", "CapReached", "RealUserRefused", "assert_synthetic_user", "is_local_pg_url", "preflight",
           "safe_error", "usage_fields", "counted_model_post", "counted_client", "install_counters",
           "build_interpreter", "score_live", "run_live_bench", "execute", "main_live", "DEFAULT_OUT"]
