#!/usr/bin/env python3
"""Food agent bench harness (WP8): replay scripted dialogues through the REAL canvas turn and score them.

A bench is a labels file (cases: gold items, expected action, recorded old-app
outcome) plus scripts (per case: what a CORRECT model would emit for each user
turn, the fake resolver catalog, seeded history). Each case runs through
``CanvasService.turn`` with the food agent enabled:

* the model is a scripted interpreter (typed ``AgentTurn`` objects),
* food lookups are the real ``FoodFinder`` over a fake resolver,
* drafts are ``MemoryDrafts`` (lane A, offline) or ``StoreDrafts`` on a
  throwaway PostgreSQL (lane B),
* history recall is the real ``HistoryRecaller`` (memory double or real Store).

This proves the code does the right thing GIVEN a correct interpretation. It
does not prove the model interprets correctly (that is lane C, not run here).
Latency is "local code time, scripted model, mocked lookups".

Script format (per case id)::

    {"turns": [{"model": null | {"actions": [...]} | {"switch": [...], "default": ...},
                "confirm_before": false}, ...],       # one entry per labeled user turn
     "catalog": [{"queries": [...], "row": {name, brand, serving, grams, kcal, p, c, f, fiber}}],
     "options": [{"query": "...", "names": ["..."]}],  # close provider names for a miss
     "history": [{"name", "days_ago", "kcal", "p", "c", "f", "fiber", "source", "target"}],
     "scoring": {"expected_entries": 1, "accept_items": [0], "label_conflict": "why"}}

Placeholders inside ``model``: ``@open:N`` (item_ref of the Nth item of the open
draft, from the context a real model would see), ``@logged:N`` (same for the
last logged meal), ``@open_ref`` / ``@open_rev`` (draft_ref and revision).
``model: null`` means no model call is expected (a plain yes). If the model is
called anyway, the harness answers with a ``confirm`` hint (what a model does
with a stray yes) and counts an unexpected model call.

The harness appends a synthetic "yes" (flagged ``final``) when the dialogue
ends with a preview awaiting confirmation: the real dialogues were with the old
app, which saved without asking. A script may also set ``confirm_before`` to
make the user confirm a shown preview before the next message (flag
``confirm_before``).

CLI::

    python food_agent_bench.py --bench file.json [--scripts s.json] --lane A --out results.json
    python food_agent_bench.py --bench labels.json --scripts scripts.json --lane B --pg-admin-url URL --out r.json
    python food_agent_bench.py --bench labels.json --scripts scripts.json --lane C --pg-admin-url LOCAL_URL \
        [--repeats 3] [--max-model-calls 300] [--max-lookups 200] [--out OUTSIDE_THE_REPO.json]

Lane C (live model and providers) is in ``food_agent_bench_live.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

LATENCY_LABEL = "local code time, scripted model, mocked lookups"
DECOY_MARKER = "B-SECRET"
CLARIFY_ACTIONS = frozenset({"clarify", "recall_not_found_ask"})

# ---------------------------------------------------------------- loading


def load_bench(labels_path: str | Path, scripts_path: str | Path | None = None) -> dict[str, Any]:
    """``{"cases": [...], "scripts": {id: {...}}}``. One file may hold both."""
    labels = json.loads(Path(labels_path).read_text())
    scripts = labels.get("scripts")
    if scripts_path is not None:
        raw = json.loads(Path(scripts_path).read_text())
        scripts = raw.get("scripts", raw)
    cases = labels["cases"]
    missing = [c["id"] for c in cases if c["id"] not in (scripts or {})]
    if missing:
        raise ValueError(f"no script for cases: {missing}")
    return {"cases": cases, "scripts": scripts, "source": str(labels_path)}


# ---------------------------------------------------------------- fake world: resolver rows, clock, doubles


def fatsecret_row(spec: dict[str, Any]) -> dict[str, Any]:
    """A resolver result in the shape of ``food_lookup._fatsecret_result``."""
    macros = {"calories": spec["kcal"], "protein": spec["p"], "carbs": spec["c"], "fat": spec["f"]}
    if spec.get("fiber") is not None:
        macros["fiber"] = spec["fiber"]
    grams = spec.get("grams")
    external = re.sub(r"[^a-z0-9]+", "-", f"{spec.get('brand', '')} {spec['name']}".casefold()).strip("-")
    row = {
        "name": spec["name"], "brand_name": spec.get("brand") or "", "macros_per_serving": macros,
        "serving_size": spec["serving"], "source": f"FatSecret: {external}",
        "attribution": {
            "provider": "FatSecret", "external_id": external, "verification_state": "provider_exact_identity",
            "source_url": None, "serving_grams": grams, "serving_basis": "per_serving", "confidence": 0.9,
            "cache_allowed": False, "attribution_text": "Nutrition data from FatSecret", "evidence_hash": "0" * 64,
        },
    }
    if grams:
        row["macros_per_100g"] = {k: round(v * 100 / grams, 2) for k, v in macros.items()}
    return row


def norm_query(query: Any) -> str:
    text = str(query or "").casefold().replace("'", "").replace("’", "").replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


class ScriptedResolver:
    def __init__(self, catalog: list[dict[str, Any]]):
        self.table: dict[str, dict[str, Any]] = {}
        for entry in catalog:
            row = fatsecret_row(entry["row"])
            for query in entry["queries"]:
                self.table[norm_query(query)] = row
        self.queries: list[str] = []

    async def __call__(self, query: str):
        self.queries.append(query)
        return self.table.get(norm_query(query))


class FixedClock:
    def __init__(self):
        self.now = datetime(2026, 11, 2, 17, 30, tzinfo=timezone.utc)  # 12:30 New York

    def __call__(self) -> datetime:
        return self.now


class OldPath:
    """The pre-existing canvas model. A food-agent turn must never reach it."""

    def __init__(self):
        self.calls: list[str] = []
        self.writes: list[str] = []  # write attempts, so a double write by the old model is visible

    async def __call__(self, **kwargs):
        self.calls.append(kwargs["message"])
        write = (kwargs.get("handlers") or {}).get("log_meal")
        if write is not None:  # worst case: the old model logs whatever it was handed
            self.writes.append(kwargs["message"])
            try:
                await write({"description": kwargs["message"]})
            except Exception:  # the harness' refusing handler raises; the attempt is what matters
                pass
        return "Handed to the old path.", []


class MemoryChat:
    def __init__(self):
        self.history: list[dict[str, str]] = []

    async def fetch_chat_messages_since(self, *_):
        return list(self.history)

    async def insert_user_chat_message(self, text, *, daily_cap):
        self.history.append({"role": "user", "content": text})

    async def insert_chat_message(self, role, text, *_):
        self.history.append({"role": role, "content": text})

    async def insert_coach_usage(self, *_):
        pass


# ---------------------------------------------------------------- scripted interpreter


def _matches(when: dict[str, Any], context: dict[str, Any]) -> bool:
    open_ = (context.get("open_drafts") or [None])[0]
    if "has_open" in when and bool(open_) != bool(when["has_open"]):
        return False
    if "awaiting_yes" in when and bool(open_ and open_.get("awaiting_yes")) != bool(when["awaiting_yes"]):
        return False
    if "question" in when and not re.search(when["question"], (open_ or {}).get("question") or "", re.I):
        return False
    return True


def _pick(spec: Any, context: dict[str, Any]) -> Any:
    if isinstance(spec, dict) and "switch" in spec:
        for branch in spec["switch"]:
            if _matches(branch.get("when", {}), context):
                return _pick(branch.get("turn"), context)
        return _pick(spec.get("default"), context)
    return spec


def _fill(node: Any, context: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_fill(x, context) for x in node]
    if isinstance(node, dict):
        return {k: _fill(v, context) for k, v in node.items()}
    if not (isinstance(node, str) and node.startswith("@")):
        return node
    open_ = (context.get("open_drafts") or [None])[0]
    logged = context.get("last_logged")
    if node == "@open_ref":
        return open_.get("draft_ref", "none") if open_ else "none"
    if node == "@open_rev":
        return int(open_.get("revision", 1)) if open_ else 1
    kind, _, number = node[1:].partition(":")
    source = open_ if kind == "open" else logged
    try:
        return source["items"][int(number) - 1]["item_ref"]
    except (TypeError, KeyError, IndexError, ValueError):
        return "x1"


def agent2() -> bool:
    """Whether this run drives ``food_agent2`` (``--agent 2``) instead of ``food_agent``."""
    return os.environ.get("FOOD_BENCH_AGENT") == "2"


class ScriptedSources:
    """Lanes A/B for ``food_agent2``: the script's catalog is the exact lane; nothing else exists."""

    def __init__(self, resolver: ScriptedResolver):
        self.resolver = resolver

    async def exact(self, query, lane):
        return await self.resolver(query)

    async def rows(self, query, require_all):
        return []

    async def hits(self, query):
        return []

    async def food(self, food_id, query, hit):
        for row in self.resolver.table.values():
            if row["attribution"]["external_id"] == food_id:
                return row
        return None

    async def web(self, query):
        return None


def as_agent2_turn(data: dict[str, Any]):
    """A scripted reply in the new contract. The model can no longer ask, so a scripted ask is dropped."""
    from food_agent2.contracts import SCHEMA_VERSION, Turn
    actions = []
    for action in data.get("actions") or []:
        if action.get("type") == "ask":
            continue
        actions.append({"type": "confirm"} if action.get("type") == "confirm" else action)
    return Turn.model_validate_json(json.dumps({"schema_version": SCHEMA_VERSION,
                                                "actions": actions or [{"type": "confirm"}]}))


class ScriptedInterpreter:
    """The model. ``index`` is the labeled user turn being played (None for a synthetic yes)."""

    def __init__(self, turns: list[dict[str, Any]]):
        self.turns = turns
        self.index: int | None = None
        self.unexpected = 0
        self.sent: list[list[str]] = []

    async def __call__(self, message, context):
        from food_agent.contracts import SCHEMA_VERSION, AgentTurn
        spec = self.turns[self.index].get("model") if self.index is not None and self.index < len(self.turns) \
            else None
        chosen = _pick(spec, context)
        if chosen is None:  # a stray yes: the model's honest hint, which code ignores
            self.unexpected += 1
            chosen = {"actions": [{"type": "confirm", "draft_ref": "@open_ref", "shown_revision": "@open_rev"}]}
        data = {"schema_version": SCHEMA_VERSION, **_fill(chosen, dict(context))}
        turn = as_agent2_turn(data) if agent2() else AgentTurn.model_validate_json(json.dumps(data))
        self.sent.append([action.type for action in turn.actions])
        return turn


class SpyAgent:
    """Wraps the real FoodAgent so the harness can read the typed result the canvas turned into a snapshot."""

    def __init__(self, agent):
        self.agent = agent
        self.last = None

    async def turn(self, message, *, session_ref=None, turn_ref=None):
        self.last = None
        self.last = await self.agent.turn(message, session_ref=session_ref, turn_ref=turn_ref)
        return self.last


# ---------------------------------------------------------------- backends (lane A memory, lane B Postgres)


def _num(value: Any) -> Any:
    return float(value) if isinstance(value, Decimal) else value


def norm_entry(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("component_metadata")
    if isinstance(meta, str):
        meta = json.loads(meta)
    return {
        "id": str(row["id"]), "name": row.get("name"), "meal": row.get("meal"),
        "calories": _num(row.get("calories")), "protein": _num(row.get("protein")),
        "carbs": _num(row.get("carbs")), "fat": _num(row.get("fat")), "fiber": _num(row.get("fiber")),
        "day": str(row.get("day") or row.get("date") or ""), "entry_revision": int(row.get("entry_revision") or 1),
        "macro_source": row.get("macro_source"), "component_metadata": meta,
    }


class MemoryHistory:
    """``search_own_entries`` over seeded rows (per user) plus the meals logged in this case."""

    def __init__(self, drafts, clock):
        self.drafts, self.clock = drafts, clock
        self.seeded: dict[str, list[dict[str, Any]]] = {}
        self.owner: str | None = None  # the case's user A (MemoryDrafts holds only A's meals)

    async def search_own_entries(self, terms, *, since, until=None, limit=20):
        from auth import current_user_id
        user = str(current_user_id())
        rows = list(self.seeded.get(user, []))
        if user == self.owner:
            for entry in self.drafts.entries.values():
                rows.append({**entry, "day": date.fromisoformat(str(entry["date"])[:10]),
                             "created_at": self.clock()})
        patterns = [str(t).strip().casefold() for t in terms if str(t).strip()]
        out = []
        for row in rows:
            haystack = (str(row.get("name")) + " " + json.dumps(row.get("component_metadata"), default=str)).casefold()
            day = row["day"]
            if any(p in haystack for p in patterns) and day >= since and (until is None or day <= until):
                out.append(copy.deepcopy(row))
        out.sort(key=lambda r: (r["day"], str(r.get("created_at"))), reverse=True)
        return out[:limit]


class CaseEnv:
    """Everything one case needs: two synthetic users, drafts, history, chat and a clock."""

    def __init__(self, *, user_a, user_b, drafts, history, chat, clock, today, backend):
        self.user_a, self.user_b, self.drafts, self.history = user_a, user_b, drafts, history
        self.chat, self.clock, self.today, self.backend = chat, clock, today, backend

    async def seed(self, user, rows: list[dict[str, Any]]) -> list[str]:
        return await self.backend.seed(self, user, rows)

    async def entries(self) -> list[dict[str, Any]]:
        return await self.backend.entries(self)


class MemoryBackend:
    name = "MemoryDrafts (offline)"

    async def new_case(self) -> CaseEnv:
        from food_agent.drafts import MemoryDrafts
        import domain
        clock = FixedClock()
        drafts = MemoryDrafts(clock=clock)
        history = MemoryHistory(drafts, clock)
        user_a, user_b = uuid4(), uuid4()
        history.owner = str(user_a)
        return CaseEnv(user_a=user_a, user_b=user_b, drafts=drafts, history=history, chat=MemoryChat(),
                       clock=clock, today=domain.effective_date(clock()), backend=self)

    async def seed(self, env: CaseEnv, user, rows):
        ids = []
        for row in rows:
            entry_id = str(uuid4())
            ids.append(entry_id)
            env.history.seeded.setdefault(str(user), []).append({
                "id": entry_id, "name": row["name"], "meal": "Lunch", "calories": float(row["kcal"]),
                "protein": float(row["p"]), "carbs": float(row["c"]), "fat": float(row["f"]),
                "fiber": float(row["fiber"]), "day": env.today - timedelta(days=int(row.get("days_ago", 1))),
                "created_at": env.clock(), "macro_source": row.get("source") or "Synthetic seed",
                "entry_revision": 1, "component_metadata": None})
        return ids

    async def entries(self, env: CaseEnv):
        return [norm_entry({**e, "day": e["date"]}) for e in env.drafts.entries.values()]

    async def aclose(self):
        return None


class PgBackend:
    """Lane B: a throwaway database on a local PostgreSQL (never a real one)."""

    name = "StoreDrafts (PostgreSQL)"

    def __init__(self, store, admin_url: str, db_name: str, url: str):
        self.store, self.admin_url, self.db_name, self.url = store, admin_url, db_name, url

    @classmethod
    async def create(cls, admin_url: str) -> "PgBackend":
        import asyncpg
        for flag in ("CALORIE_ONLY_WRITES_ENABLED", "CALORIE_ONLY_WRITES_USER_IDS"):
            os.environ.pop(flag, None)
        os.environ.setdefault("DATABASE_URL", "postgresql://fixture.invalid/macro_tracker")
        os.environ.setdefault("APP_SHARED_TOKEN", "bench-shared-token")
        import migrations
        from store import Store
        name = "mmm_bench_" + uuid4().hex[:12]
        admin = await asyncpg.connect(admin_url)
        await admin.execute(f'CREATE DATABASE "{name}"')
        await admin.close()
        base, _, query = admin_url.partition("?")
        url = f"{base.rsplit('/', 1)[0]}/{name}" + (f"?{query}" if query else "")
        await migrations.apply_migrations(url)  # 000-002; 003 and 004 are held in the repo
        conn = await asyncpg.connect(url)
        for held in ("003_calorie_only_unknown_macros.sql", "004_meal_drafts.sql"):
            await conn.execute((migrations.MIGRATIONS_DIR / held).read_text())
        await conn.close()

        class BenchStore(Store):
            async def connect(self):
                if self.pool is None:
                    self.pool = await asyncpg.create_pool(url, min_size=1, max_size=6)
                return self.pool

        return cls(BenchStore(url), admin_url, name, url)

    async def new_case(self) -> CaseEnv:
        from food_agent.drafts import StoreDrafts
        import domain
        pool = await self.store.connect()
        users = [uuid4(), uuid4()]
        for user in users:
            await pool.execute("INSERT INTO users(id, display_name) VALUES($1, 'Synthetic bench user')", user)
        clock = lambda: datetime.now(timezone.utc)  # noqa: E731 - the store stamps updated_at with now()
        return CaseEnv(user_a=users[0], user_b=users[1], drafts=StoreDrafts(self.store), history=self.store,
                       chat=self.store, clock=clock, today=domain.effective_date(clock()), backend=self)

    async def seed(self, env: CaseEnv, user, rows):
        from auth import bind_user, reset_user
        ids = []
        scope = bind_user(user)
        try:
            for row in rows:
                saved = await self.store.insert_meal(
                    name=row["name"], meal="Lunch", calories=float(row["kcal"]), protein=float(row["p"]),
                    carbs=float(row["c"]), fat=float(row["f"]), fiber=float(row["fiber"]),
                    day=env.today - timedelta(days=int(row.get("days_ago", 1))),
                    macro_source=row.get("source") or "Synthetic seed")
                ids.append(str(saved["id"]))
        finally:
            reset_user(scope)
        return ids

    async def entries(self, env: CaseEnv):
        pool = await self.store.connect()
        seeded = {str(i) for i in env.seeded_ids}
        rows = await pool.fetch(
            "SELECT id,name,meal,calories,protein,carbs,fat,fiber,day,entry_revision,macro_source,"
            "component_metadata FROM nutrition_entries WHERE user_id=$1 ORDER BY created_at, id", env.user_a)
        return [norm_entry(dict(r)) for r in rows if str(r["id"]) not in seeded]

    async def aclose(self):
        import asyncpg
        await self.store.aclose()
        admin = await asyncpg.connect(self.admin_url)
        await admin.execute(f'DROP DATABASE IF EXISTS "{self.db_name}" WITH (FORCE)')
        await admin.close()


# ---------------------------------------------------------------- run records


@dataclass
class TurnRecord:
    idx: int
    user: str
    synthetic: str | None = None  # None | "confirm_before" | "final"
    decision: str = "other"  # the user's message as code reads it: yes / no / other
    reply: str = ""
    is_question: bool = False
    question: str | None = None
    status: str = ""
    effect: str = ""
    handed_off: bool = False
    preview: dict[str, Any] | None = None
    receipt_id: str | None = None
    receipt_verified: bool | None = None
    error: str | None = None
    elapsed_ms: float = 0.0
    model_types: list[str] = field(default_factory=list)
    scripted_index: int | None = None
    commits: list[dict[str, Any]] = field(default_factory=list)
    entries_after: int = 0
    rows: list[dict[str, Any]] | None = None  # the saved rows right after this turn (receipts are checked against these)


@dataclass
class CaseRun:
    case_id: str
    turns: list[TurnRecord] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)
    decoy_markers: list[str] = field(default_factory=list)
    script_actions: list[dict[str, Any]] = field(default_factory=list)  # every action the model emitted
    unexpected_model_calls: int = 0
    old_path_calls: int = 0
    wall_ms: float = 0.0
    harness_error: str | None = None


# ---------------------------------------------------------------- running a case


def error_text(exc: BaseException) -> str:
    """Lanes A/B: the class and a short message (offline, no provider text can be in it)."""
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def scripted_parts(case: dict[str, Any], script: dict[str, Any], env: "CaseEnv"):
    """Lanes A/B: the scripted interpreter and the real FoodFinder over a fake resolver."""
    from food_agent.find_food import FoodFinder
    resolver = ScriptedResolver(script.get("catalog") or [])
    if agent2():
        return ScriptedInterpreter(script["turns"]), ScriptedSources(resolver)
    options = {norm_query(o["query"]): list(o["names"]) for o in script.get("options") or []}

    async def suggest(query):
        return list(options.get(norm_query(query), []))

    return ScriptedInterpreter(script["turns"]), FoodFinder(resolver, suggest=suggest, is_ambiguous=lambda _q: False)


async def run_case(case: dict[str, Any], script: dict[str, Any], backend, *, parts=None, errors=None,
                   hooks=None) -> CaseRun:
    """Play one case through ``CanvasService.turn``.

    ``parts(case, script, env) -> (interpreter, finder)`` picks the model and the lookups (default: scripted,
    lanes A/B; lane C passes the live ones). The interpreter exposes ``index``, ``sent`` and ``unexpected``.
    ``errors(exc) -> str`` formats a recorded error. ``hooks`` (optional) gets ``turn_start(index)`` and
    ``turn_end(record)`` around every turn, and ``case_ready(env)`` once the synthetic users exist.
    """
    from agent_canvas import CanvasService
    from auth import bind_user, reset_user
    from food_agent import confirm
    from food_agent.coordinator import FoodAgent
    from food_agent.recall import HistoryRecaller

    errors = errors or error_text
    wall0 = time.perf_counter()
    run = CaseRun(case_id=case["id"])
    env = await backend.new_case()
    if hooks is not None:
        hooks.case_ready(env)
    env.seeded_ids = []
    seeds = script.get("history") or []
    env.seeded_ids += await env.seed(env.user_a, seeds)
    decoys = [{"name": f"{DECOY_MARKER} {g['identity']['name']}", "days_ago": 0, "kcal": 9999, "p": 999, "c": 99,
               "f": 99, "fiber": 9, "source": f"{DECOY_MARKER} source"} for g in case.get("gold_items", [])]
    decoy_ids = await env.seed(env.user_b, decoys)
    run.decoy_markers = [DECOY_MARKER, *decoy_ids]

    interpreter, finder = (parts or scripted_parts)(case, script, env)
    if agent2():
        from food_agent2.history import HistoryRecall
        from food_agent2.retrieval import Retrieval
        from food_agent2.turn import FoodAgent2
        agent = FoodAgent2(env.drafts, interpreter, Retrieval(finder), HistoryRecall(env.history, env.clock), env.clock)
    else:
        agent = FoodAgent(env.drafts, interpreter, finder, HistoryRecaller(env.history, env.clock), env.clock)
    spy = SpyAgent(agent)
    old = OldPath()

    def foods():
        async def refuse(call_id, args):
            run.old_path_calls += 1
            raise AssertionError("the old food path must not run for a food-agent turn")
        return {"log_meal": refuse, "lookup_food": refuse}

    async def factory():
        return spy

    service = CanvasService(store_factory=lambda: env.chat, food_factory=foods, coach_factory=lambda: {},
                            agent=old, food_agent_factory=factory)
    session_id = str(uuid4())
    scope = bind_user(env.user_a)
    try:
        before = {e["id"]: e["entry_revision"] for e in await env.entries()}

        async def say(text: str, index: int | None, synthetic: str | None) -> TurnRecord:
            nonlocal before
            interpreter.index = index
            sent0 = len(interpreter.sent)
            record = TurnRecord(idx=len(run.turns), user=text, synthetic=synthetic, scripted_index=index,
                                decision=confirm.classify(text))
            if hooks is not None:
                hooks.turn_start(record.idx)
            t0 = time.perf_counter()
            try:
                snapshot = await service.turn(session_id, str(uuid4()), text, adapter="text")
            except Exception as exc:  # an error is an outcome: timed and recorded
                snapshot, record.error = None, errors(exc)
            record.elapsed_ms = (time.perf_counter() - t0) * 1000
            result = spy.last
            record.model_types = [t for turn_types in interpreter.sent[sent0:] for t in turn_types]
            if snapshot is not None:
                record.reply = str(snapshot.get("reply") or "")
                receipt = snapshot.get("receipt") or {}
                record.receipt_id = receipt.get("recordId") if isinstance(receipt, dict) else None
            if result is not None:
                record.is_question = result.question is not None
                record.question = result.question
                record.status, record.effect = result.outcome.status, result.outcome.effect
                record.handed_off = result.handed_off
                if result.preview is not None:
                    record.preview = {"draft_id": result.preview.draft_id, "revision": result.preview.revision,
                                      "content_hash": result.preview.content_hash}
                if result.receipt is not None:
                    record.receipt_verified = result.receipt.verified
                    record.receipt_id = record.receipt_id or result.receipt.entry_id
            now = await env.entries()
            record.commits = [
                {"entry_id": e["id"], "revision": e["entry_revision"],
                 "draft_id": (e.get("component_metadata") or {}).get("draft_id"),
                 "content_hash": (e.get("component_metadata") or {}).get("content_hash")}
                for e in now if e["id"] not in before or e["entry_revision"] > before[e["id"]]]
            record.entries_after = len(now)
            record.rows = copy.deepcopy(now)
            before = {e["id"]: e["entry_revision"] for e in now}
            run.turns.append(record)
            if hooks is not None:
                hooks.turn_end(record)  # lane C: may raise its cap stop (a BaseException) here
            return record

        def awaiting_yes(record: TurnRecord) -> bool:
            # the canvas snapshot keeps the previous receipt, so only this turn's own result counts
            return record.preview is not None and not record.commits and record.receipt_verified is None \
                and not record.is_question

        for index, user_turn in enumerate(case["user_turns"]):
            if script["turns"][index].get("confirm_before") and run.turns and awaiting_yes(run.turns[-1]):
                await say("yes", None, "confirm_before")
            await say(user_turn, index, None)
        if case.get("expected_action") not in CLARIFY_ACTIONS and run.turns and awaiting_yes(run.turns[-1]):
            await say("yes", None, "final")
        run.entries = await env.entries()
    except Exception as exc:  # a harness crash, never a score
        run.harness_error = errors(exc)
        run.entries = run.entries or []
    finally:
        reset_user(scope)
    run.old_path_calls += len(old.calls)  # the old model ran at all: a fail, with or without a write
    run.unexpected_model_calls = interpreter.unexpected
    run.script_actions = [{"turn": t.idx, "types": t.model_types} for t in run.turns]
    run.wall_ms = (time.perf_counter() - wall0) * 1000
    return run


# ---------------------------------------------------------------- scoring


_TOKEN = re.compile(r"[a-z]+|\d+")
_UNIT_TOKENS = frozenset({"oz", "fl", "g", "ml", "lb", "l", "kg", "cup", "tbsp", "tsp"})
_DESCRIPTORS = frozenset({"the", "a", "an", "of", "and"})  # filler only: size, raw/cooked and flavor words are identity
_NUMBERED = re.compile(r"(^|\s)\d+\s*[.)](\s|$)")
_CLAIM_START = re.compile(r"\s*(logged|saved|updated)\b", re.I)  # a receipt-style sentence: "Logged X: ..."
_VERB = r"(?:logged|saved|added|updated)"
_CLAIM_ANYWHERE = re.compile(
    rf"\bi(?:'ve|\s+have)?\s+(?:just\s+|already\s+)?{_VERB}\b"  # "I logged", "I've saved"
    rf"|\b{_VERB}\s+(?:it|that|this|them)\b"  # "logged it (for you)", "saved it", "added it"
    rf"|\b(?:it|that|this|they)(?:'s|\s+is|\s+was|\s+has\s+been|\s+have\s+been|\s+are)\s+(?:been\s+)?{_VERB}\b"
    rf"|\b(?:has|have)\s+been\s+{_VERB}\b", re.I)
_NEGATOR = re.compile(r"n't\b|\bnot\b|\bnever\b|\bcannot\b|\bno longer\b|\bnothing\b", re.I)
_AMOUNT_Q = re.compile(r"\bhow (many|much)\b", re.I)


def _stem(token: str) -> str:
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def _tokens(text: Any) -> list[str]:
    text = str(text or "").casefold().replace("'", "").replace("’", "").replace("&", " and ")
    return [_stem(t) for t in _TOKEN.findall(text)]


def identity_matches(gold: dict[str, Any], display_name: str) -> bool:
    """Independent oracle: preserve preparation, species, flavor and numeric ratios.

    Do not infer a brand from an arbitrary prefix or discard unexplained words.
    A label may explicitly declare accepted display names; missing independent
    evidence remains a failure rather than being made true by production output.
    """
    wanted = set(_tokens(gold.get("brand"))) | set(_tokens(gold.get("name")))
    wanted |= set(_tokens(gold.get("variant"))) | set(_tokens(gold.get("preparation")))
    wanted -= _DESCRIPTORS
    shown = set(_tokens(display_name)) - _DESCRIPTORS
    if wanted == {"egg"} and not gold.get("brand") and shown == {"chicken", "egg"}:
        return True  # a chicken egg, not a different species or cooked preparation
    # Preparation is required, not merely allowed among additional descriptors.
    constraints = set(_tokens(gold.get("preparation"))) | set(_tokens(gold.get("variant")))
    constraints |= set(_tokens(gold.get("brand")))
    constraints -= _DESCRIPTORS  # ``shown`` drops these too: "Chick-fil-A" must not require a lone "a"
    if not constraints <= shown:
        return False
    if wanted == shown and wanted:
        return True
    return bool(wanted) and any(
        shown == set(_tokens(alias)) - _DESCRIPTORS
        for alias in gold.get("accepted_names", [])
    )


def _expected(scoring: dict[str, Any], gold: dict[str, Any], index: int, key: str) -> float | None:
    """A hand-derived expected value for gold item ``index``: on the gold item itself or in the script's
    ``scoring[key]`` map. The scorer never derives it with the portion math under test."""
    if gold.get(key) is not None:
        return float(gold[key])
    given = (scoring.get(key) or {}).get(str(index))
    return None if given is None else float(given)


def consumed_matches(gold, saved, expected_grams=None):
    """Compare consumed units against the ACTUAL serving, not a scripted multiplier.

    None means the evidence does not define comparable units; it is not success.
    This oracle intentionally does not import the production portion calculator.
    """
    basis = saved.get("serving_basis") or {}
    mult = float(saved.get("multiplier") or 0)
    portion = gold.get("portion") or {}
    amount = portion.get("amount")
    if amount is None:
        return None
    amount = float(amount)
    mass = {"g": 1, "kg": 1000, "oz": 28.349523125, "lb": 453.59237}
    volume = {"ml": 1, "l": 1000, "fl oz": 29.5735295625, "fl_oz": 29.5735295625,
              "cup": 236.5882365, "tbsp": 14.78676478125, "tsp": 4.92892159375}
    def near(actual, expected):
        return actual is not None and abs(float(actual) - expected) <= max(.01, abs(expected) * .02)
    if expected_grams is not None:
        per = basis.get("grams") or (float(basis.get("amount") or 0) * mass.get(basis.get("unit"), 0))
        return near(float(per) * mult if per else None, expected_grams)
    kind, unit = portion.get("kind"), portion.get("unit")
    if kind in ("mass", "volume"):
        scales = mass if kind == "mass" else volume
        if unit not in scales:
            return None
        per = basis.get("grams" if kind == "mass" else "ml")
        if per is None and basis.get("unit") in scales:
            per = float(basis.get("amount") or 0) * scales[basis["unit"]]
        return near(float(per) * mult if per else None, amount * scales[unit])
    if kind == "count":
        noun = str(unit or "").lower().rstrip("s")
        text = str(basis.get("text") or "").lower()
        # Explicit counted units can sit inside a multi-unit serving, e.g. 1 order (3 tacos).
        found = re.search(r"(\d+(?:\.\d+)?)\s+(?:large |small |medium )?" + re.escape(noun) + r"s?\b", text) if noun else None
        if found:
            return near(float(found[1]) * mult, amount)
        if basis.get("unit") == "count" and basis.get("count_noun") and noun in str(basis["count_noun"]).lower():
            return near(float(basis.get("amount") or 0) * mult, amount)
        return None
    if kind == "serving_fraction":
        # Fractions mean the named serving, not an arbitrary per-100g panel.
        if basis.get("basis", "per_serving") != "per_serving":
            return None
        if basis.get("unit") == "count" and basis.get("amount"):
            return near(float(basis["amount"]) * mult, amount)
        if re.search(r"^1 (?:logged portion|serving)\b", str(basis.get("text") or "")):
            return near(mult, amount)
        return None
    return None


def _saved_items(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for entry in entries:
        meta = entry.get("component_metadata") or {}
        for item in meta.get("items") or []:
            out.append({**item, "_entry": entry})
    return out


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def claims_logged(reply: str) -> bool:
    """Does the reply claim this turn logged/saved/updated/added something?

    A receipt-style sentence ("Logged X, no sauce: ...") always counts. A first-person or "it" claim ("I logged
    it", "saved it") counts unless a negator sits right before the verb ("I haven't logged it"). A question never
    claims.
    """
    for sentence in _sentences(str(reply or "").replace("’", "'")):
        if sentence.rstrip().endswith("?"):
            continue
        if _CLAIM_START.match(sentence):
            return True
        for found in _CLAIM_ANYWHERE.finditer(sentence):
            before = " ".join(sentence[:found.end()].split()[-5:-1])  # up to 4 words ahead of the claim verb
            if not _NEGATOR.search(sentence[max(0, found.start() - 25):found.end()]) and not _NEGATOR.search(before):
                return True
    return False


def _numbers(reply: str) -> dict[str, float]:
    out = {}
    for key, pattern in (("calories", r"([\d.]+) kcal"), ("protein", r"([\d.]+) g protein")):
        found = re.search(pattern, reply)
        if found:
            try:
                out[key] = float(found.group(1))
            except ValueError:
                pass
    return out


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    data = sorted(values)
    pos = (len(data) - 1) * q / 100
    low = int(pos)
    high = min(low + 1, len(data) - 1)
    return data[low] + (data[high] - data[low]) * (pos - low)


def _counted(n: int, d: int) -> dict[str, int]:
    return {"n": n, "d": d}


def score_case(case: dict[str, Any], script: dict[str, Any], run: CaseRun, *, live_basis: bool = False) -> dict[str, Any]:
    """Score one run (pure). ``success`` follows AGENT.md section 8; every failure has a reason."""
    scoring = (script or {}).get("scoring") or {}
    gold_items = case.get("gold_items") or []
    expect_log = case.get("expected_action") not in CLARIFY_ACTIONS
    accepted = scoring.get("accept_items")
    accepted = list(range(len(gold_items))) if accepted is None else list(accepted)
    expected_entries = int(scoring.get("expected_entries", 1 if expect_log else 0))
    reasons: list[str] = []
    saved = _saved_items(run.entries)

    # ---- item identity and portion
    used: set[int] = set()
    identity_ok = portion_ok = user_ok = user_d = 0
    lost_fraction = 0
    item_rows = []
    gold_all = [g["identity"] for g in gold_items]
    for g in (accepted if expect_log else []):
        gold = gold_items[g]
        candidates = [i for i, s in enumerate(saved) if i not in used
                      and identity_matches(gold["identity"], str(s.get("display_name") or ""))]
        best = None
        expected = _expected(scoring, gold, g, "expected_multiplier")
        expected_grams = _expected(scoring, gold, g, "expected_grams")
        if expected is None and not live_basis:
            reasons.append(f"no expected_multiplier for gold item {g} (the portion cannot be checked)")

        for i in candidates:
            saved_mult = float(saved[i].get("multiplier") or 0)
            consumed = consumed_matches(gold, saved[i], gold.get("expected_grams") if live_basis else expected_grams)
            good = consumed is True if live_basis else expected is not None and abs(saved_mult - expected) <= 0.01 * expected
            if good and expected_grams is not None and not live_basis:
                per = (saved[i].get("serving_basis") or {}).get("grams")
                grams_ok = per is not None and abs(float(per) * saved_mult - expected_grams) <= 0.02 * expected_grams
                if not grams_ok:
                    reasons.append(f"grams: gold item {g} expected about {expected_grams:g} g")
                good = grams_ok
            if best is None or (good and not best[2]):
                best = (i, expected, good)
        row = {"gold": g, "identity": best is not None, "portion": bool(best and best[2]), "expected_multiplier":
               best[1] if best else None, "saved_multiplier": saved[best[0]].get("multiplier") if best else None}
        if best is not None:
            used.add(best[0])
            identity_ok += 1
            portion_ok += 1 if best[2] else 0
            exp = best[1]
            if exp is not None and abs(exp - round(exp)) > 0.005 and not best[2]:
                lost_fraction += 1
                reasons.append(f"lost fraction on gold item {g}")
        supplied = gold.get("user_supplied_calories") or (gold.get("user_supplied_panel") or {}).get("calories")
        if supplied is not None:
            user_d += 1
            got = (saved[best[0]].get("nutrients") or {}).get("calories") if best else None
            row["user_calories"] = {"supplied": supplied, "saved": got}
            if got is not None and abs(float(got) - float(supplied)) <= 0.5:
                user_ok += 1
            else:
                reasons.append(f"user-supplied calories {supplied} not used (saved {got})")
        item_rows.append(row)
    d_items = len(accepted) if expect_log else 0
    if expect_log and identity_ok < d_items:
        reasons.append(f"identity: {d_items - identity_ok} gold item(s) not logged with the right food")
    if expect_log and portion_ok < d_items:
        reasons.append(f"portion: {d_items - portion_ok} gold item(s) with the wrong amount")

    extras = [s for i, s in enumerate(saved) if i not in used]
    dup_items = sum(1 for s in extras if any(identity_matches(g, str(s.get("display_name") or "")) for g in gold_all))
    rows = len(run.entries)
    duplicates = max(0, rows - expected_entries) + (dup_items if rows <= expected_entries else 0)
    if rows != expected_entries:
        reasons.append(f"rows: {rows} saved, {expected_entries} expected")
    if expect_log and extras:
        reasons.append(f"{len(extras)} extra saved item(s) the user did not ask for")

    # ---- wrong past meal: a copy of history that is not one of the gold foods
    wrong_past = any(s.get("provider") == "history" and not any(
        identity_matches(g, str(s.get("display_name") or "")) for g in gold_all) for s in saved)
    if wrong_past:
        reasons.append("wrong past meal copied from history")

    if any(t.status in ("error", "unknown") or t.error for t in run.turns):
        reasons.append("interpreter or infrastructure error is not a quality success")

    # Independent golden consumed totals, not receipt/row self-consistency.
    expected_nutrients = case.get("expected_nutrients")
    if expect_log:
        if not expected_nutrients:
            reasons.append("independent gold nutrition unavailable (not verified)")
        else:
            for key, expected_value in expected_nutrients.items():
                values = [e.get(key) for e in run.entries]
                if not values or any(v is None for v in values) or abs(sum(float(v) for v in values) - float(expected_value)) > .55:
                    reasons.append(f"gold nutrition mismatch: {key}")

    if run.old_path_calls:
        reasons.append(f"old path ran ({run.old_path_calls} call/write attempt(s)) for a food-agent dialogue")

    # ---- confirm sequence, stale commits, false claims, receipts
    committed_before_confirm = false_logged = stale = receipt_bad = 0
    for turn in run.turns:
        if turn.commits and turn.decision != "yes":
            committed_before_confirm += 1
        if claims_logged(turn.reply) and not turn.commits:
            false_logged += 1
        for commit in turn.commits:
            prior = run.turns[turn.idx - 1] if turn.idx else None
            shown = [prior.preview] if prior and prior.preview and prior.preview["draft_id"] == commit["draft_id"] else []
            if not shown or shown[-1]["content_hash"] != commit["content_hash"]:
                stale += 1
            # the row as that turn left it: a later amend of the same entry must not retroactively fail this receipt
            written = turn.rows if turn.rows is not None else run.entries
            entry = next((e for e in written if e["id"] == commit["entry_id"]), None)
            said = _numbers(turn.reply)
            if entry is None or not said or any(
                    entry.get(k) is None or abs(float(entry[k]) - v) > 0.55 for k, v in said.items()):
                receipt_bad += 1
            elif turn.receipt_id is not None and str(turn.receipt_id) != commit["entry_id"]:
                receipt_bad += 1
    for label, count in (("a row was saved before the user's confirming turn", committed_before_confirm),
                         ("false 'logged' claim without a new row", false_logged),
                         ("a commit that was not the previewed version", stale),
                         ("receipt numbers differ from the saved row", receipt_bad)):
        if count:
            reasons.append(label)

    blob = json.dumps([t.reply for t in run.turns]) + json.dumps(run.entries, default=str)
    leaks = sorted({m for m in run.decoy_markers if m and m in blob})
    if leaks:
        reasons.append("cross-tenant leak: another user's entry appeared")

    # ---- questions and re-asks
    question_turns = [t for t in run.turns if t.is_question]
    model_ask = [t for t in question_turns if "ask" in t.model_types]
    reasked = _reasked(run)
    numbered = any(_NUMBERED.search(t.reply) for t in run.turns)

    if expect_log:
        if not run.entries:
            reasons.append("nothing was logged")
    else:
        if not question_turns:
            reasons.append("expected a clarifying question, got none")
        elif not run.turns[-1].reply.rstrip().endswith("?"):
            reasons.append("the final reply is not a question")
        expected_question = case.get("expected_question")
        if not expected_question:
            reasons.append("expected clarification meaning unavailable")
        else:
            # Explicit expected concepts, not just a question mark. Gold may name
            # alternatives per concept; all concepts must be represented.
            concepts = expected_question if isinstance(expected_question, list) else [expected_question]
            actual = " ".join(t.reply.lower() for t in question_turns if t.status not in ("error", "unknown"))
            for concept in concepts:
                alternatives = concept if isinstance(concept, list) else [concept]
                if not any(set(_tokens(term)) <= set(_tokens(actual)) for term in alternatives):
                    reasons.append("wrong clarification meaning")
                    break
        if numbered:
            reasons.append("a numbered list in the reply")
        if run.entries:
            reasons.append("rows were written for a clarify case")
    if run.harness_error:
        reasons.append(f"harness error: {run.harness_error}")

    return {
        "id": case["id"], "category": case.get("category"), "expected_action": case.get("expected_action"),
        "success": not reasons,
        "success_strict": not reasons and not any(t.synthetic for t in run.turns),
        # Behavior only: right food, right amount, saved once, honest replies. Reachable
        # while labels still lack independent gold nutrition; never a substitute for it.
        "success_behavior": all("not verified" in r for r in reasons),
        "reasons": reasons, "label_conflict": scoring.get("label_conflict"),
        "identity": _counted(identity_ok, d_items), "portion": _counted(portion_ok, d_items),
        "user_numbers": _counted(user_ok, user_d), "items": item_rows,
        "rows": rows, "expected_rows": expected_entries,
        "counters": {"false_logged": false_logged, "duplicates": duplicates, "stale_version_commits": stale,
                     "cross_tenant_leaks": len(leaks), "committed_before_confirm": committed_before_confirm},
        "failure_classes": {"wrong_past_meal": int(wrong_past), "lost_fraction": int(lost_fraction > 0),
                            "reasked_confirmed_item": int(reasked)},
        "questions": {"total": len(question_turns), "model_authored": len(model_ask),
                      "code_authored": len(question_turns) - len(model_ask)},
        "unexpected_model_calls": run.unexpected_model_calls, "old_path_calls": run.old_path_calls,
        "synthetic_confirms": sum(1 for t in run.turns if t.synthetic),
        "turn_ms": [round(t.elapsed_ms, 3) for t in run.turns],
        "case_ms": round(sum(t.elapsed_ms for t in run.turns), 3), "case_wall_ms": round(run.wall_ms, 3),
        "harness_error": run.harness_error, "old_app": case.get("old_app") or {},
    }


def _reasked(run: CaseRun) -> bool:
    """The agent (not a scripted Ask) asks again about something the user already answered or confirmed."""
    asked: dict[str, int] = {}
    amount_given = False
    for turn in run.turns:
        if turn.is_question and "ask" not in turn.model_types and not turn.handed_off:
            text = (turn.question or turn.reply).strip().casefold()
            if turn.decision == "yes":
                return True  # the user said yes and got a question back
            if text in asked and any(t.model_types and set(t.model_types) & {"add_items", "revise_item"}
                                     for t in run.turns[asked[text] + 1:turn.idx + 1]):
                return True
            if _AMOUNT_Q.search(text) and amount_given:
                return True
            asked.setdefault(text, turn.idx)
        if set(turn.model_types) & {"add_items", "revise_item"}:
            amount_given = amount_given or turn.scripted_index is not None
    return False


# ---------------------------------------------------------------- summary


def summarize(cases: list[dict[str, Any]], lane: str, backend_name: str) -> dict[str, Any]:
    def total(key):
        return _counted(sum(c[key]["n"] for c in cases), sum(c[key]["d"] for c in cases))

    zero = {k: sum(c["counters"][k] for c in cases) for k in
            ("false_logged", "duplicates", "stale_version_commits", "cross_tenant_leaks", "committed_before_confirm")}
    old = [c["old_app"] for c in cases]

    def old_sum(key):
        return sum(1 for o in old if o.get(key))

    classes = {k: {"agent": sum(c["failure_classes"][k] for c in cases), "old_app": old_sum(k)}
               for k in ("wrong_past_meal", "lost_fraction", "reasked_confirmed_item")}
    turn_ms = [ms for c in cases for ms in c["turn_ms"]]
    case_ms = [c["case_ms"] for c in cases]
    conflicts = [c for c in cases if c.get("label_conflict")]
    decided = [c for c in cases if not c.get("label_conflict")]
    return {
        "case_success": _counted(sum(1 for c in cases if c["success"]), len(cases)),
        "case_success_strict": _counted(sum(1 for c in cases if c["success_strict"]), len(cases)),
        "case_success_excluding_label_conflicts": _counted(sum(1 for c in decided if c["success"]), len(decided)),
        "label_conflict_cases": [c["id"] for c in conflicts],
        "identity": total("identity"), "portion": total("portion"), "user_numbers": total("user_numbers"),
        "zero_tolerance": zero, "failure_classes": classes,
        "questions": {"total": sum(c["questions"]["total"] for c in cases),
                      "model_authored": sum(c["questions"]["model_authored"] for c in cases),
                      "code_authored": sum(c["questions"]["code_authored"] for c in cases),
                      "per_case": round(sum(c["questions"]["total"] for c in cases) / max(1, len(cases)), 3),
                      "old_app_total": sum(int(o.get("questions_asked") or 0) for o in old),
                      "old_app_per_case": round(sum(int(o.get("questions_asked") or 0) for o in old)
                                                / max(1, len(cases)), 3)},
        "latency_ms": {"label": LATENCY_LABEL,
                       "turn": {"n": len(turn_ms), "p50": _r(percentile(turn_ms, 50)), "p95": _r(percentile(turn_ms, 95))},
                       "case": {"n": len(case_ms), "p50": _r(percentile(case_ms, 50)), "p95": _r(percentile(case_ms, 95))}},
        "old_app": {"case_success": _counted(old_sum("case_success"), len(cases)),
                    "false_logged": old_sum("false_logged"), "duplicate": old_sum("duplicate"),
                    "wrong_past_meal": old_sum("wrong_past_meal"), "lost_fraction": old_sum("lost_fraction"),
                    "reasked_confirmed_item": old_sum("reasked_confirmed_item"),
                    "questions_asked": sum(int(o.get("questions_asked") or 0) for o in old)},
        "unexpected_model_calls": sum(c["unexpected_model_calls"] for c in cases),
        "synthetic_confirms": sum(c["synthetic_confirms"] for c in cases),
        "old_path_calls": sum(c["old_path_calls"] for c in cases),
        "lane": lane, "drafts_backend": backend_name,
    }


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


async def run_bench(spec: dict[str, Any], backend, *, lane: str) -> dict[str, Any]:
    results = []
    for case in spec["cases"]:
        script = spec["scripts"][case["id"]]
        run = await run_case(case, script, backend)
        scored = score_case(case, script, run)
        scored["transcript"] = [{"user": t.user, "synthetic": t.synthetic, "reply": t.reply,
                                 "question": t.is_question, "status": t.status, "model": t.model_types,
                                 "rows_after": t.entries_after, "error": t.error} for t in run.turns]
        results.append(scored)
    return {"version": 1, "lane": lane, "label": LATENCY_LABEL, "source": spec.get("source"),
            "generated_at": datetime.now(timezone.utc).isoformat(), "cases": results,
            "summary": summarize(results, lane, getattr(backend, "name", "?"))}


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bench", required=True, help="labels file (or a file holding cases and scripts)")
    parser.add_argument("--scripts", help="scripts file when separate from the labels")
    parser.add_argument("--lane", choices=("A", "B", "C"), default="A",
                        help="A: offline; B: throwaway PostgreSQL; C: LIVE providers (food_agent_bench_live)")
    parser.add_argument("--pg-admin-url", default=os.environ.get("MMMACROS_TEST_PG_ADMIN_URL", ""))
    parser.add_argument("--out", help="results file (required for A/B; lane C has a private default)")
    parser.add_argument("--repeats", type=int, default=3, help="lane C: runs per case")
    parser.add_argument("--max-model-calls", type=int, default=300, help="lane C: hard cap on model attempts")
    parser.add_argument("--max-lookups", type=int, default=200, help="lane C: hard cap on provider attempts")
    parser.add_argument("--rate-card", help="lane C: reviewed nonsecret versioned JSON route prices")
    parser.add_argument("--agent", choices=("1", "2"), default="1", help="1: food_agent; 2: food_agent2")
    args = parser.parse_args(argv)
    os.environ["FOOD_BENCH_AGENT"] = args.agent
    if args.lane == "C":
        import food_agent_bench_live  # noqa: PLC0415 - lane C lives in its own module
        return food_agent_bench_live.main_live(args)
    if not args.out:
        parser.error("--out is required for lanes A and B")
    spec = load_bench(args.bench, args.scripts)

    async def go():
        if args.lane == "B":
            if not args.pg_admin_url:
                raise SystemExit("lane B needs --pg-admin-url (a throwaway local PostgreSQL)")
            backend = await PgBackend.create(args.pg_admin_url)
        else:
            backend = MemoryBackend()
        try:
            return await run_bench(spec, backend, lane=args.lane)
        finally:
            await backend.aclose()

    results = asyncio.run(go())
    Path(args.out).write_text(json.dumps(results, indent=2))
    print(json.dumps(results["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
