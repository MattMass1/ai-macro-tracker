"""Lane C (live provider evaluation) harness, proven OFFLINE with fakes only.

Nothing here touches the network: the model ``post``, the FatSecret HTTP
client, the Exa/web post functions and the CoFID fetch are all fakes, and an
autouse guard fails the test if any real httpx transport is reached. The
harness itself is never run live from this repo's tests; DevOps runs it.
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.parse
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import food_agent_bench as bench  # noqa: E402
import food_agent_bench_live as live  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "food_agent_bench_synthetic.json"
ADMIN_URL = os.environ.get("MMMACROS_TEST_PG_ADMIN_URL", "")
pg = pytest.mark.skipif(not ADMIN_URL, reason="set MMMACROS_TEST_PG_ADMIN_URL to a throwaway local PostgreSQL")
REPO = Path(__file__).resolve().parents[2]
FAKE_SECRET = "sk-FAKE-SECRET-7f3a9c"  # never a real key; it must never surface anywhere


# ---------------------------------------------------------------- fakes


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Any real HTTP attempt is recorded and fails the test at teardown."""
    reached: list[str] = []

    async def blocked(self, request):
        reached.append(request.url.host)
        raise httpx.ConnectError("real network is blocked in tests", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", FAKE_SECRET)
    for key in ("FATSECRET_ATTRIBUTION_ENABLED", "FATSECRET_CONSUMER_KEY", "FATSECRET_CONSUMER_SECRET",
                "FATSECRET_CLIENT_ID", "FATSECRET_CLIENT_SECRET", "FOOD_AGENT_MODEL", "COACH_MODEL"):
        monkeypatch.delenv(key, raising=False)
    # All credentials and prices below are synthetic test fixtures, never a live plan.
    from food_agent_budget import RateCard, TERRA
    rates = RateCard(version="synthetic-offline-only", providers={"fatsecret": 0, "exa": 0, "cofid": 0, "usda": 0},
                    evidence={p: "synthetic fixture" for p in ("fatsecret", "exa", "cofid", "usda")},
                    hosted_models={"synthetic-hosted": {"version": "fixture", "source": "synthetic fixture",
                        "input_per_million": 1, "output_per_million": 5, "max_context_tokens": 32000,
                        "tool_per_call_usd": .01}})
    monkeypatch.setattr(live, "RateCard", lambda: rates)
    for key, value in {"FOOD_AGENT_MODEL": TERRA, "COACH_MODEL": "synthetic-hosted",
                       "WEB_LOOKUP_EXTRACT_MODEL": TERRA, "WEB_LOOKUP_PROVIDER": "exa",
                       "EXA_API_KEY": "synthetic", "USDA_API_KEY": "synthetic",
                       "FATSECRET_ATTRIBUTION_ENABLED": "true",
                       "FATSECRET_CLIENT_ID": "synthetic", "FATSECRET_CLIENT_SECRET": "synthetic"}.items():
        monkeypatch.setenv(key, value)
    enable_fake_fatsecret(monkeypatch, _spec())
    yield reached
    assert reached == [], "a real network call was attempted"


def _spec(*ids):
    spec = bench.load_bench(FIXTURE)
    if ids:
        spec["cases"] = [c for c in spec["cases"] if c["id"] in ids]
    return spec


def _response(status=200, body=None, url="https://fake.invalid/v1/chat/completions"):
    return httpx.Response(status, json=body if body is not None else {}, request=httpx.Request("POST", url))


class FakeModel:
    """A fake ``post``: answers what the script says a correct model emits for the user message it is sent."""

    def __init__(self, spec, *, model_id="fake-model-1", usage=True):
        from food_agent.contracts import SCHEMA_VERSION
        self.schema_version = SCHEMA_VERSION
        self.by_message: dict[str, object] = {}
        for case in spec["cases"]:
            for index, text in enumerate(case["user_turns"]):
                model = spec["scripts"][case["id"]]["turns"][index].get("model")
                if model is not None:
                    self.by_message.setdefault(text, model)
        self.model_id, self.usage = model_id, usage
        self.payloads: list[dict] = []

    async def __call__(self, token, payload):
        self.payloads.append(payload)
        content = payload["input"][1]["content"]
        head, _, message = content.rpartition("\n\nMessage:\n")
        context = json.loads(head.split("\n", 1)[1])
        chosen = bench._pick(self.by_message.get(message), context)
        if chosen is None:
            chosen = {"actions": [{"type": "confirm", "draft_ref": "@open_ref", "shown_revision": "@open_rev"}]}
        turn = {"schema_version": self.schema_version, **bench._fill(chosen, context)}
        body = {"id": "resp-fake", "model": self.model_id, "status": "completed",
                "output": [{"type": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": json.dumps(turn)}]}]}
        if self.usage:
            body["usage"] = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        return _response(200, body)


def fatsecret_transport(spec, *, status=None):
    """A fake FatSecret REST endpoint built from the scripts' catalogs (foods.search + food.get.v4)."""
    table, foods = {}, {}
    for script in spec["scripts"].values():
        for entry in script.get("catalog") or []:
            row = entry["row"]
            fid = str(1000 + len(foods))
            foods[fid] = row
            for query in entry["queries"]:
                table[bench.norm_query(query)] = fid
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        calls.append(form.get("method", "?"))
        if status is not None:
            return httpx.Response(status, request=request)
        if form.get("method") == "foods.search":
            fid = table.get(bench.norm_query(form.get("search_expression")))
            hits = [] if fid is None else [{"food_id": fid, "food_name": foods[fid]["name"],
                                            "brand_name": foods[fid].get("brand") or ""}]
            return httpx.Response(200, json={"foods_search": {"results": {"food": hits}}}, request=request)
        row = foods.get(str(form.get("food_id")))
        if row is None:
            return httpx.Response(200, json={}, request=request)
        serving = {"serving_description": row["serving"], "metric_serving_amount": str(row["grams"]),
                   "metric_serving_unit": "g", "calories": str(row["kcal"]), "protein": str(row["p"]),
                   "carbohydrate": str(row["c"]), "fat": str(row["f"]), "fiber": str(row["fiber"])}
        return httpx.Response(200, json={"food": {"food_id": form["food_id"], "food_name": row["name"],
                                                  "brand_name": row.get("brand") or "",
                                                  "servings": {"serving": [serving]}}}, request=request)

    return httpx.MockTransport(handler), calls


def enable_fake_fatsecret(monkeypatch, spec, *, status=None):
    import food_lookup
    transport, calls = fatsecret_transport(spec, status=status)
    monkeypatch.setenv("FATSECRET_ATTRIBUTION_ENABLED", "true")
    monkeypatch.setenv("FATSECRET_CONSUMER_KEY", "fake-consumer-key")
    monkeypatch.setenv("FATSECRET_CONSUMER_SECRET", FAKE_SECRET)
    monkeypatch.setattr(food_lookup, "_client", lambda: httpx.AsyncClient(transport=transport))
    return calls


class FakeWeb:
    def __init__(self, status=503, body=None):
        self.status, self.body, self.calls = status, body, 0

    async def __call__(self, key, payload):
        self.calls += 1
        return _response(self.status, self.body or {"error": {"message": f"denied {FAKE_SECRET}"}},
                         url="https://fake.invalid/v1/responses")


async def _no_cofid():
    import cofid_lookup
    raise cofid_lookup.CoFIDFetchError("fake: no workbook offline")


class LocalStoreStub:
    """``server.store_client()`` for offline runs: no presets, no tenant catalog."""

    async def fetch_presets(self, active_only=True):
        return []

    async def lookup_catalog(self, query):
        return None

    async def cache_provider_food(self, found):
        return None


def fake_usda(spec):
    """A fake FoodData Central search built from the scripts' UNBRANDED catalog rows (the whole-food lane)."""
    foods = []
    for script in spec["scripts"].values():
        for entry in script.get("catalog") or []:
            row = entry["row"]
            if row.get("brand"):
                continue
            foods.append({"fdcId": 500000 + len(foods), "dataType": "SR Legacy", "description": row["name"],
                          "foodNutrients": [{"nutrientId": i, "value": round(v * 100 / row["grams"], 2)} for i, v in
                                            ((1008, row["kcal"]), (1003, row["p"]), (1005, row["c"]),
                                             (1004, row["f"]), (1079, row["fiber"]))],
                          "foodMeasures": [{"disseminationText": row["serving"], "gramWeight": row["grams"]}]})

    async def post(params):
        wanted = set(bench.norm_query(params["query"]).split())
        hits = [f for f in foods if wanted <= set(bench.norm_query(f["description"]).split())]
        return httpx.Response(200, json={"foods": hits}, request=httpx.Request("GET", "https://fake.invalid/fdc"))
    return post


def _providers(model, *, web=None, exa=None, usda=None, spec=None):
    return {"model_post": model, "web_post": web or FakeWeb(), "exa_post": exa or FakeWeb(),
            "cofid_fetch": _no_cofid, "local_store": LocalStoreStub(),
            "usda_post": usda or fake_usda(spec or _spec())}


async def _run(spec, *, budget=None, repeats=1, model=None, **kw):
    budget = budget or live.Budget(max_model_calls=300, max_lookups=200)
    model = model or FakeModel(spec)
    providers = _providers(model, spec=spec, **kw)
    results = await live.run_live_bench(spec, bench.MemoryBackend(), budget, repeats=repeats, **providers)
    return results, budget, model


# ---------------------------------------------------------------- end to end with fakes


async def test_live_harness_runs_the_real_turn_with_fake_providers_and_records_everything(monkeypatch):
    spec = _spec("syn-half-burger", "syn-boiled-eggs-control", "syn-last-time-bowl")
    fs_calls = enable_fake_fatsecret(monkeypatch, spec)
    results, budget, model = await _run(spec, repeats=2)
    runs = results["runs"]
    assert [r["status"] for r in runs] == ["run"] * 6
    by_case = results["per_case"]
    for case_id in ("syn-half-burger", "syn-boiled-eggs-control", "syn-last-time-bowl"):
        assert by_case[case_id]["with_yes"] == "2/2", (case_id, [r["score"]["reasons"] for r in runs
                                                                 if r["case_id"] == case_id])
    burger = next(r for r in runs if r["case_id"] == "syn-half-burger")
    (item,) = burger["items_audit"]
    assert item["provider"] == "FatSecret" and item["external_id"] and item["multiplier"] == 0.5
    assert item["raw_display_name"] and item["macro_source"].startswith("FatSecret:")
    assert item["matches_gold"] == [0]
    assert "fatsecret" in {c["provider"] for c in burger["resolver_calls"]}
    usda_calls = [a for a in budget.lookup_attempts if a["provider"] == "usda"]
    assert fs_calls and usda_calls  # both lanes ran: eggs went to USDA, the burger to FatSecret
    assert budget.lookups_used == len(fs_calls) + len(usda_calls)  # every wire attempt is counted once
    call = burger["model_calls"][0]
    assert call["status"] == "ok" and call["model"] == "fake-model-1"
    assert (call["prompt_tokens"], call["completion_tokens"], call["total_tokens"]) == (100, 20, 120)
    assert call["latency_ms"] >= 0 and call["requested_model"] == "gpt-6.1-terra"
    summary = results["summary"]
    assert summary["model_calls"] == budget.model_calls_used == len(model.payloads)
    assert summary["tokens"]["tokens_known_calls"] == budget.model_calls_used
    assert summary["tokens"]["tokens_unknown_calls"] == 0
    assert summary["tokens"]["total_tokens"] == 120 * budget.model_calls_used
    assert summary["latency_ms"]["label"] == "live"
    assert summary["latency_ms"]["turn"]["n"] == sum(len(r["transcript"]) for r in runs)
    assert summary["runs"] == {"planned": 6, "completed": 6, "not_run_cap": 0, "not_run_configuration": 0}
    assert summary["case_success"]["n"] <= summary["case_success"]["d"] == 6
    for counter in ("false_logged", "duplicates", "stale_version_commits", "cross_tenant_leaks",
                    "committed_before_confirm"):
        assert counter in summary["zero_tolerance"]
    json.dumps(results)


# ---------------------------------------------------------------- (a) model cap


async def test_model_cap_stops_at_exactly_n_attempts_and_marks_the_rest_not_run():
    spec = _spec()  # half-burger (2 model calls), smoothie, bowl, eggs
    budget = live.Budget(max_model_calls=2, max_lookups=200)
    results, budget, model = await _run(spec, budget=budget, repeats=2)
    assert len(model.payloads) == 2 and budget.model_calls_used == 2
    assert results["summary"]["stopped_by_cap"] == "model"
    statuses = [(r["case_id"], r["repeat"], r["status"]) for r in results["runs"]]
    assert statuses[0] == ("syn-half-burger", 1, "run")
    assert all(s == "not run (cap)" for _, _, s in statuses[1:]) and len(statuses) == 8
    interrupted = results["runs"][1]
    assert interrupted["interrupted"] is True and "score" not in interrupted  # not scored as a failure
    assert results["summary"]["runs"] == {"planned": 8, "completed": 1, "not_run_cap": 7, "not_run_configuration": 0}
    assert results["summary"]["case_success"]["d"] == 8  # over every planned case x repeat run
    assert results["per_case"]["syn-smoothie-six-oz"]["not_run_cap"] == 2


async def test_model_cap_counts_every_wire_attempt_including_a_retry():
    import coach
    seen = []

    async def flaky(token, payload):
        seen.append(1)
        return _response(503 if len(seen) == 1 else 200, {"model": "m", "choices": []})

    budget = live.Budget(max_model_calls=2, max_lookups=1)
    counted = live.counted_model_post(flaky, budget)
    await coach._call_provider(counted, "t", {"model": "gpt-6.1-terra", "input": "synthetic", "max_output_tokens": 2000})  # retries once on 503
    assert budget.model_calls_used == 2 and [c["http_status"] for c in budget.model_calls] == [503, 200]
    assert budget.model_calls[0]["status"] == "error"
    with pytest.raises(live.CapReached):
        await counted("t", {"model": "gpt-6.1-terra", "input": "synthetic", "max_output_tokens": 2000})
    assert len(seen) == 2  # the refused call was never made
    assert budget.refused == {"model": 1, "lookup": 0}


# ---------------------------------------------------------------- (b) lookup cap


async def test_lookup_cap_stops_at_exactly_n_provider_attempts_counting_retries(monkeypatch):
    spec = _spec("syn-half-burger", "syn-boiled-eggs-control")
    calls = enable_fake_fatsecret(monkeypatch, spec, status=503)  # every attempt fails; FatSecret retries
    web = FakeWeb(status=503)
    budget = live.Budget(max_model_calls=300, max_lookups=4)
    results, budget, _ = await _run(spec, budget=budget, repeats=1, web=web)
    assert len(calls) + web.calls == 4 == budget.lookups_used  # exactly N attempts were made, then refused
    fatsecret = [a for a in budget.lookup_attempts if a["provider"] == "fatsecret"]
    assert len(fatsecret) == len(calls) >= 3  # one request's retries are each counted
    assert all(a["http_status"] == 503 and a["status"] == "error" for a in budget.lookup_attempts)
    assert budget.refused["lookup"] >= 1
    assert results["summary"]["stopped_by_cap"] == "lookup"
    assert [r["status"] for r in results["runs"]] == ["not run (cap)", "not run (cap)"]
    assert results["summary"]["lookups"] == 4


def test_counted_fatsecret_client_sees_each_retry():
    budget = live.Budget(max_model_calls=1, max_lookups=10)
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(500 if len(attempts) < 3 else 200, json={}, request=request)

    client = live.counted_client(lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)), budget)

    async def go():
        import food_lookup
        async with client() as c:
            await food_lookup._request_with_backoff(c, "POST", "https://platform.fatsecret.com/rest/x")

    import asyncio
    asyncio.run(go())
    assert budget.lookups_used == 3 == len(attempts)


# ---------------------------------------------------------------- (c) local-only database


@pytest.mark.parametrize("url", [
    "postgresql://u@localhost/x", "postgresql://u:p@127.0.0.1:5432/x", "postgresql://u@[::1]:5432/x",
    "postgresql:///x", "postgresql://u@/x?host=/tmp/pg", "postgresql://u@%2Ftmp%2Fpg/x",
    "postgres://u@LOCALHOST/x", "/tmp/pgsock",
])
def test_local_urls_are_accepted(url):
    assert live.is_local_pg_url(url, env={}) is True


@pytest.mark.parametrize("url", [
    "postgresql://u:p@dpg-abc123-a.oregon-postgres.render.com/db", "postgresql://u@10.0.0.5/x",
    "postgresql://u@db.example.com/x", "postgresql://u@localhost.example.com/x",
    "postgresql://u@localhost,db.example.com/x", "postgresql://u@/x?host=db.example.com",
    "postgresql://u@localhost/x?host=10.0.0.5", "postgresql://u@localhost/x?hostaddr=10.0.0.5",
    "mysql://u@localhost/x", "not a url", "",
])
def test_non_local_urls_are_refused(url):
    assert live.is_local_pg_url(url, env={}) is False


def test_empty_host_falls_back_to_pghost_which_must_be_local_too():
    assert live.is_local_pg_url("postgresql:///x", env={"PGHOST": "db.example.com"}) is False
    assert live.is_local_pg_url("postgresql:///x", env={"PGHOST": "/var/run/postgresql"}) is True


def test_preflight_refuses_a_remote_database_url_without_printing_it(tmp_path):
    remote = "postgresql://user:hunter2@dpg-xyz.oregon-postgres.render.com/prod"
    errors = live.preflight(pg_admin_url="postgresql://u@localhost/x", out=tmp_path / "r.json",
                            env={"DATABASE_URL": remote})
    assert "DATABASE_URL is not local" in errors
    assert "DATABASE_URL is not local" not in live.preflight(pg_admin_url="postgresql://u@localhost/x", out=tmp_path / "r.json",
                          env={"DATABASE_URL": "postgresql://u@127.0.0.1/app"})
    assert live.preflight(pg_admin_url="postgresql://u@localhost/x", out=tmp_path / "r.json", env={})
    bad = live.preflight(pg_admin_url="postgresql://u:pw@10.0.0.5/x", out=tmp_path / "r.json", env={})
    assert "--pg-admin-url is not local" in bad and "10.0.0.5" not in " ".join(bad)
    assert "lane C needs --pg-admin-url (a throwaway local PostgreSQL)" in live.preflight(pg_admin_url="", out=tmp_path / "r.json", env={})


@pytest.mark.parametrize("admin,database_url,message", [
    ("postgresql://u@db.example.com/x", None, "--pg-admin-url is not local"),
    ("postgresql://u@10.0.0.5/x", None, "--pg-admin-url is not local"),
    ("postgresql://u@localhost/x", "postgresql://a:b@dpg-q.render.com/prod", "DATABASE_URL is not local"),
])
def test_cli_refuses_before_any_connection(monkeypatch, tmp_path, capsys, admin, database_url, message):
    import asyncpg

    async def no_connect(*a, **k):
        raise AssertionError("connected despite refusal")

    monkeypatch.setattr(asyncpg, "connect", no_connect)
    monkeypatch.setattr(asyncpg, "create_pool", no_connect)
    if database_url is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", database_url)
    out = tmp_path / "results-laneC.json"
    code = bench.main(["--lane", "C", "--bench", str(FIXTURE), "--pg-admin-url", admin, "--out", str(out)])
    captured = capsys.readouterr()
    assert code != 0 and not out.exists()
    assert message in captured.err
    assert admin not in captured.err + captured.out and (database_url or "@@") not in captured.err + captured.out


# ---------------------------------------------------------------- (d) usage missing is null, not 0


async def test_missing_provider_usage_is_stored_as_null_not_zero():
    spec = _spec("syn-boiled-eggs-control")
    results, budget, _ = await _run(spec, model=FakeModel(spec, usage=False))
    calls = results["runs"][0]["model_calls"]
    assert calls and all(c["prompt_tokens"] is None and c["completion_tokens"] is None
                         and c["total_tokens"] is None for c in calls)
    tokens = results["summary"]["tokens"]
    assert tokens["tokens_known_calls"] == 0 and tokens["tokens_unknown_calls"] == len(calls)
    assert tokens["prompt_tokens"] is None and tokens["total_tokens"] is None


def test_partial_usage_keeps_known_fields_and_nulls_the_rest():
    record = live.usage_fields({"usage": {"prompt_tokens": 12}, "model": "gpt-x"})
    assert record == {"model": "gpt-x", "prompt_tokens": 12, "completion_tokens": None, "total_tokens": None}
    assert live.usage_fields({"usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})[
        "total_tokens"] == 0  # a real provider zero stays zero
    assert live.usage_fields("not json")["model"] is None


# ---------------------------------------------------------------- (e) scoring a live resolved name


def _qp_case():
    gold = {"identity": {"name": "Quarter Pounder", "brand": "McDonald's", "variant": "with cheese",
                         "kind": "restaurant", "preparation": None},
            "portion": {"kind": "count", "amount": 1, "unit": "sandwich"}, "modifications": []}
    case = {"id": "qp", "expected_action": "log", "gold_items": [gold], "old_app": {}, "user_turns": ["qp"],
            "expected_nutrients": {"calories": 520, "protein": 30}}
    script = {"scoring": {"expected_entries": 1, "expected_multiplier": {"0": 1}}}
    return case, script


def _qp_run(display_name, multiplier):
    item = {"display_name": display_name, "multiplier": multiplier, "provider": "FatSecret",
            "external_id": "4242", "food_id": "FatSecret:4242", "macro_source": "FatSecret: 4242",
            "serving_basis": {"grams": None, "text": "1 sandwich", "unit": "count", "amount": 1}}
    entry = {"id": "e1", "name": display_name, "calories": 520.0, "protein": 30.0, "entry_revision": 1,
             "component_metadata": {"draft_id": "d1", "content_hash": "h", "items": [item]}}
    shown = bench.TurnRecord(idx=0, user="qp", preview={"draft_id": "d1", "revision": 1, "content_hash": "h"})
    commit = bench.TurnRecord(idx=1, user="yes", decision="yes", synthetic="final",
                              reply="Logged it: 30 g protein, 520 kcal.",
                              commits=[{"entry_id": "e1", "revision": 1, "draft_id": "d1", "content_hash": "h"}],
                              rows=[entry])
    return bench.CaseRun(case_id="qp", turns=[shown, commit], entries=[entry], decoy_markers=["B-SECRET"])


def test_live_resolved_name_is_scored_against_the_gold_identity_and_multiplier():
    case, script = _qp_case()
    brands = {"FatSecret:4242": "McDonald's"}
    ok = live.score_live(case, script, _qp_run("Quarter Pounder with Cheese", 1.0), brands)
    assert ok["identity"] == {"n": 1, "d": 1} and ok["portion"] == {"n": 1, "d": 1}
    assert ok["items_audit"][0]["raw_display_name"] == "Quarter Pounder with Cheese"
    assert ok["items_audit"][0]["brand"] == "McDonald's" and ok["items_audit"][0]["provider"] == "FatSecret"
    assert ok["items_audit"][0]["external_id"] == "4242" and ok["items_audit"][0]["matches_gold"] == [0]
    assert ok["success"] and not ok["success_strict"]  # the final yes was synthetic

    double = live.score_live(case, script, _qp_run("Double Quarter Pounder with Cheese", 1.0), brands)
    assert double["identity"] == {"n": 0, "d": 1} and not double["success"]

    half = live.score_live(case, script, _qp_run("Quarter Pounder with Cheese", 0.5), brands)
    assert half["identity"] == {"n": 1, "d": 1} and half["portion"] == {"n": 0, "d": 1}

    unbranded = live.score_live(case, script, _qp_run("Quarter Pounder with Cheese", 1.0), {})
    assert unbranded["identity"] == {"n": 0, "d": 1}  # no brand evidence: not the McDonald's item
    already = live.score_live(case, script, _qp_run("McDonald's Quarter Pounder with Cheese", 1.0),
                              {"FatSecret:4242": "McDonald's"})
    assert already["identity"] == {"n": 1, "d": 1}  # the brand is never doubled


# ---------------------------------------------------------------- (f) results written on a cap


async def test_results_are_written_with_counts_when_a_cap_is_hit(tmp_path):
    spec = _spec()
    out = tmp_path / "results-laneC.json"
    budget = live.Budget(max_model_calls=1, max_lookups=200)
    model = FakeModel(spec)
    stdout = io.StringIO()
    code = await live.execute(spec, bench.MemoryBackend(), budget, out=out, repeats=3, stdout=stdout,
                              **_providers(model))
    assert code == 0
    data = json.loads(out.read_text())
    assert data["lane"] == "C" and data["summary"]["stopped_by_cap"] == "model"
    assert data["summary"]["caps"] == {"max_model_calls": 1, "max_lookups": 200, "model_calls_used": 1,
                                       "lookups_used": budget.lookups_used,
                                       "refused": {"model": 1, "lookup": 0}}
    assert data["summary"]["runs"]["not_run_cap"] == 12 and data["summary"]["runs"]["planned"] == 12
    assert "not run (cap)" in stdout.getvalue()


# ---------------------------------------------------------------- (g) secrets never leak


async def test_a_provider_error_holding_a_secret_never_reaches_results_or_stdout(monkeypatch, tmp_path):
    spec = _spec("syn-half-burger", "syn-boiled-eggs-control")
    seen = []

    async def leaky_model(token, payload):
        seen.append(token)
        if len(seen) % 2:
            raise httpx.ConnectError(f"connect failed https://api.example/?key={FAKE_SECRET}")
        return _response(401, {"error": {"message": f"Incorrect API key provided: {FAKE_SECRET}"},
                               "model": f"m-{FAKE_SECRET}"})

    web = FakeWeb(status=401)
    out = tmp_path / "results-laneC.json"
    stdout = io.StringIO()
    budget = live.Budget(max_model_calls=300, max_lookups=200)
    await live.execute(spec, bench.MemoryBackend(), budget, out=out, repeats=1, stdout=stdout,
                       **{**_providers(leaky_model, web=web), "model_post": leaky_model})
    assert seen and seen[0] == FAKE_SECRET  # the key was read from the process env and sent
    text = out.read_text() + stdout.getvalue()
    assert FAKE_SECRET not in text
    errors = {c["error"] for r in json.loads(out.read_text())["runs"] for c in r["model_calls"]}
    assert errors <= {"ConnectError", "http_401"}
    assert all(c["model"] is None or FAKE_SECRET not in c["model"]
               for r in json.loads(out.read_text())["runs"] for c in r["model_calls"])


def test_safe_error_keeps_the_class_and_a_short_code_only():
    request = httpx.Request("GET", f"https://x.invalid/?key={FAKE_SECRET}")
    status = httpx.HTTPStatusError(f"bad {FAKE_SECRET}", request=request,
                                   response=httpx.Response(403, request=request))
    assert live.safe_error(status) == "HTTPStatusError: http_403"
    assert live.safe_error(httpx.ReadTimeout(f"timeout {FAKE_SECRET}")) == "ReadTimeout"
    assert live.safe_error(RuntimeError(FAKE_SECRET)) == "RuntimeError"


# ---------------------------------------------------------------- (h) Matthew's real id is refused


def test_matthews_real_id_is_refused():
    with pytest.raises(live.RealUserRefused):
        live.assert_synthetic_user("be6333cc-e4c1-48f2-adb1-5e7f14dbf7c2")
    with pytest.raises(live.RealUserRefused):
        live.assert_synthetic_user("BE6333CC-E4C1-48F2-ADB1-5E7F14DBF7C2")
    live.assert_synthetic_user("9b2f0a54-0c0e-4b33-9d55-1f1f6a1d2c3e")


async def test_a_backend_handing_out_matthews_id_stops_the_run():
    from uuid import UUID

    class RealIdBackend(bench.MemoryBackend):
        async def new_case(self):
            env = await super().new_case()
            env.user_b = UUID(live.MATTHEW_REAL_USER_ID)
            return env

    spec = _spec("syn-boiled-eggs-control")
    model = FakeModel(spec)
    with pytest.raises(live.RealUserRefused):
        await live.run_live_bench(spec, RealIdBackend(), live.Budget(300, 200), repeats=1, **_providers(model))
    assert model.payloads == []  # refused before any model call


# ---------------------------------------------------------------- (i) --out inside the repo is refused


def test_out_inside_the_worktree_is_refused(tmp_path):
    inside = REPO / "server" / "results-laneC.json"
    errors = live.preflight(pg_admin_url="postgresql://u@localhost/x", out=inside, env={})
    assert "--out is inside the git worktree" in errors
    assert live.preflight(pg_admin_url="postgresql://u@localhost/x", out=tmp_path / "r.json", env={})


def test_cli_refuses_out_inside_the_worktree(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    inside = REPO / "results-laneC-should-not-exist.json"
    code = bench.main(["--lane", "C", "--bench", str(FIXTURE), "--pg-admin-url", "postgresql://u@localhost/x",
                       "--out", str(inside)])
    assert code != 0 and not inside.exists()
    assert "--out is inside the git worktree" in capsys.readouterr().err


def test_lane_c_default_out_is_the_private_bench_dir():
    assert str(live.DEFAULT_OUT) == ("/Users/matthewagent/.hermes/profiles/devops/reports/"
                                     "mmmacros-agent-build/bench/results-laneC.json")


# ---------------------------------------------------------------- (j) model selection order


@pytest.mark.parametrize("env,expected", [
    ({"FOOD_AGENT_MODEL": "food-m", "COACH_MODEL": "coach-m"}, "food-m"),
    ({"COACH_MODEL": "coach-m"}, "gpt-6.1-terra"),
    ({}, "gpt-6.1-terra"),
])
async def test_model_selection_order_is_what_the_adapter_sends(monkeypatch, env, expected):
    monkeypatch.delenv("FOOD_AGENT_MODEL", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sent = []

    async def fake(token, payload):
        sent.append(payload["model"])
        return _response(500, {})

    budget = live.Budget(max_model_calls=5, max_lookups=5)
    from food_agent.model_adapter import ModelInterpreter
    interpreter = ModelInterpreter(post=fake)  # adapter routing, independent of live price gate
    from food_agent.model_adapter import InterpreterError
    with pytest.raises(InterpreterError):
        await interpreter("2 eggs", {})
    assert sent == [expected]


# ---------------------------------------------------------------- stdout carries ids and numbers only


async def test_stdout_has_case_ids_and_scores_but_no_message_text_or_food_names(monkeypatch, tmp_path):
    spec = _spec("syn-half-burger", "syn-boiled-eggs-control")
    enable_fake_fatsecret(monkeypatch, spec)
    stdout = io.StringIO()
    await live.execute(spec, bench.MemoryBackend(), live.Budget(300, 200), out=tmp_path / "r.json", repeats=1,
                       stdout=stdout, **_providers(FakeModel(spec)))
    text = stdout.getvalue()
    assert "syn-half-burger" in text and "syn-boiled-eggs-control" in text
    forbidden = [t for c in spec["cases"] for t in c["user_turns"] if len(t) > 3] + [
        g["identity"]["name"] for c in spec["cases"] for g in c["gold_items"]] + ["Harbor", "Smokehouse", "bacon"]
    assert not [w for w in forbidden if w.casefold() in text.casefold()]


# ---------------------------------------------------------------- real PostgreSQL (optional)


@pg
async def test_lane_c_on_a_throwaway_postgres_with_fake_providers(monkeypatch):
    spec = _spec("syn-boiled-eggs-control", "syn-last-time-bowl")
    backend = await bench.PgBackend.create(ADMIN_URL)
    try:
        model = FakeModel(spec)
        providers = {**_providers(model), "local_store": backend.store}
        results = await live.run_live_bench(spec, backend, live.Budget(300, 200), repeats=1, **providers)
    finally:
        await backend.aclose()
    assert [r["status"] for r in results["runs"]] == ["run", "run"]
    assert results["summary"]["zero_tolerance"]["cross_tenant_leaks"] == 0
    assert results["per_case"]["syn-last-time-bowl"]["with_yes"] == "1/1", results["runs"][1]["score"]["reasons"]
