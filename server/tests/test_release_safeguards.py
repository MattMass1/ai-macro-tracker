"""Release safeguards for unknown (NULL) nutrients: compatibility gate + rollback.

Unit tests always run. PostgreSQL tests run only when MMMACROS_TEST_PG_ADMIN_URL
points at a THROWAWAY local server; each test creates and drops its own
database. Never point it at a shared, staging or production database.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

import migrations
import release_gates
from auth import bind_user, reset_user
from test_live_coach import FakeClientWebSocket, FakeVoiceStore, _import_server

SERVER = Path(__file__).resolve().parents[1]
ADMIN_URL = os.environ.get("MMMACROS_TEST_PG_ADMIN_URL", "")
NUTRIENTS = ("protein", "carbs", "fat", "fiber")
M003 = migrations.NULLABLE_NUTRIENTS_MIGRATION


def load_script(name):
    path = SERVER / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def gates_default(monkeypatch):
    for name in ("CALORIE_ONLY_WRITES_ENABLED", "CALORIE_ONLY_WRITES_USER_IDS", "APPROVED_MIGRATIONS"):
        monkeypatch.delenv(name, raising=False)


def _with_contract(value):
    return release_gates.bind_client_contract({"x-nutrient-contract": value} if value else {})


# --------------------------------------------------------------------------- #
# Compatibility gate (unit)
# --------------------------------------------------------------------------- #

def test_gate_is_closed_by_default_even_for_a_nullable_client():
    token = _with_contract("nullable-v1")
    scope = bind_user(uuid4())
    try:
        assert release_gates.client_reads_nullable_nutrients() is True
        assert release_gates.calorie_only_write_allowed() is False
    finally:
        reset_user(scope)
        release_gates.reset_client_contract(token)


@pytest.mark.parametrize("value,opens", [
    ("true", True), (" TRUE ", True), ("True", True),
    ("1", False), ("yes", False), ("on", False), ("false", False), ("", False), ("truee", False),
])
def test_operator_switch_accepts_only_the_exact_word_true(monkeypatch, value, opens):
    monkeypatch.setenv("CALORIE_ONLY_WRITES_ENABLED", value)
    assert release_gates.calorie_only_writes_enabled() is opens


@pytest.mark.parametrize("header,declared", [
    (None, False), ("", False), ("nullable-v2", False), ("nullable", False),
    ("nullable-v1", True), ("NULLABLE-V1", True), ("other, nullable-v1", True),
])
def test_client_contract_must_declare_nullable_v1(monkeypatch, header, declared):
    monkeypatch.setenv("CALORIE_ONLY_WRITES_ENABLED", "true")
    token = _with_contract(header)
    scope = bind_user(uuid4())
    try:
        assert release_gates.client_reads_nullable_nutrients() is declared
        assert release_gates.calorie_only_write_allowed() is declared
    finally:
        reset_user(scope)
        release_gates.reset_client_contract(token)


def test_canary_list_limits_users_and_malformed_list_never_widens(monkeypatch):
    allowed, other = uuid4(), uuid4()
    monkeypatch.setenv("CALORIE_ONLY_WRITES_ENABLED", "true")
    token = _with_contract("nullable-v1")
    try:
        monkeypatch.setenv("CALORIE_ONLY_WRITES_USER_IDS", f" {allowed} ")
        for user, expected in ((allowed, True), (other, False)):
            scope = bind_user(user)
            try:
                assert release_gates.calorie_only_write_allowed() is expected
            finally:
                reset_user(scope)
        monkeypatch.setenv("CALORIE_ONLY_WRITES_USER_IDS", f"{allowed},not-a-uuid")
        scope = bind_user(allowed)
        try:
            assert release_gates.calorie_only_write_allowed() is False
        finally:
            reset_user(scope)
        # No bound user: a canary list cannot be satisfied.
        monkeypatch.setenv("CALORIE_ONLY_WRITES_USER_IDS", str(allowed))
        assert release_gates.calorie_only_write_allowed() is False
    finally:
        release_gates.reset_client_contract(token)


@pytest.mark.asyncio
async def test_http_route_binds_and_resets_the_client_contract(monkeypatch):
    srv = _import_server(monkeypatch)
    from starlette.requests import Request
    seen = []

    @srv.api_route("/api/__release_gate_probe", methods=["GET"], public=True)
    async def probe(_request):
        seen.append(release_gates.client_reads_nullable_nutrients())
        return {"ok": True}

    route = next(r for r in srv.mcp._additional_http_routes if r.path == "/api/__release_gate_probe")
    for header, expected in ((b"nullable-v1", True), (None, False)):
        headers = [(b"x-nutrient-contract", header)] if header else []
        request = Request({"type": "http", "method": "GET", "path": "/api/__release_gate_probe",
                           "headers": headers, "query_string": b""})
        response = await route.endpoint(request)
        assert response.status_code == 200
        assert "X-Nutrient-Contract" in response.headers["Access-Control-Allow-Headers"]
        assert seen[-1] is expected
        assert release_gates.client_reads_nullable_nutrients() is False  # reset after the request


@pytest.mark.asyncio
async def test_live_websocket_binds_contract_for_the_whole_session():
    from live_coach import LiveCoachService
    tenant = uuid4()

    class Store:
        async def fetch_targets(self, day): return None
        async def fetch_meals(self, day, end=None): return []
        async def fetch_workout_plan(self): return None
        async def fetch_workouts(self, start=None, end=None, exercise=None): return []
        async def fetch_prs(self): return []
        async def fetch_workout_library(self): return []

    service = LiveCoachService(store=Store(), provider_connect=None, api_key="fixture-key")
    observed = []

    async def bound(*_args):
        observed.append(release_gates.client_reads_nullable_nutrients())
    service._serve_bound = bound
    for header, expected in (("nullable-v1", True), ("", False)):
        socket = FakeClientWebSocket(authorization="Bearer fixture-only")
        if header:
            socket.headers["x-nutrient-contract"] = header
        await service._serve_admitted(socket, tenant, None)
        assert observed[-1] is expected
        assert release_gates.client_reads_nullable_nutrients() is False


class _SchemaConn:
    def __init__(self, nullable):
        self.nullable = nullable
        self.queries = 0

    async def fetchval(self, _sql, *_args):
        self.queries += 1
        return self.nullable


@pytest.mark.asyncio
async def test_store_guard_refuses_unknown_nutrients_unless_every_gate_is_open(monkeypatch):
    from store import CalorieOnlyWriteBlocked, _guard_unknown_nutrients
    unknown = {"protein": None, "carbs": None, "fat": None, "fiber": None}
    complete = {"protein": 1, "carbs": 2, "fat": 3, "fiber": 0}
    schema = _SchemaConn(False)
    await _guard_unknown_nutrients(schema, complete)  # complete rows always pass
    assert schema.queries == 0, "complete rows need no schema round trip"
    with pytest.raises(CalorieOnlyWriteBlocked):
        await _guard_unknown_nutrients(_SchemaConn(True), unknown)  # switch off
    with pytest.raises(CalorieOnlyWriteBlocked):  # ONE unknown nutrient is enough to gate
        await _guard_unknown_nutrients(_SchemaConn(True), {**complete, "fiber": None})
    monkeypatch.setenv("CALORIE_ONLY_WRITES_ENABLED", "true")
    token = _with_contract("nullable-v1")
    scope = bind_user(uuid4())
    try:
        with pytest.raises(CalorieOnlyWriteBlocked):
            await _guard_unknown_nutrients(_SchemaConn(False), unknown)  # schema not ready
        await _guard_unknown_nutrients(_SchemaConn(True), unknown)  # every gate open
    finally:
        reset_user(scope)
        release_gates.reset_client_contract(token)


async def _voice_calorie_only(srv, fake, call_id="cal"):
    scope = bind_user(uuid4())
    try:
        return await srv._voice_tool_handlers()["log_meal"](call_id, {"description": "200 calories"})
    finally:
        reset_user(scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("switch,header,schema_ready", [
    ("true", None, True),            # old native client: no contract header
    ("true", "nullable-v1", False),  # 003 not applied (held) or rolled back
    ("true", "nullable-v1", None),   # store cannot report schema state
    ("", "nullable-v1", True),       # operator switch off (forward-disable)
])
async def test_voice_asks_and_writes_nothing_when_any_gate_is_closed(monkeypatch, switch, header, schema_ready):
    srv = _import_server(monkeypatch)
    fake = FakeVoiceStore()
    if schema_ready is not None:
        async def ready():
            return schema_ready
        fake.nullable_nutrients_ready = ready
    monkeypatch.setattr(srv, "_client", fake)
    monkeypatch.setenv("CALORIE_ONLY_WRITES_ENABLED", switch)
    token = _with_contract(header)
    try:
        result = await _voice_calorie_only(srv, fake)
    finally:
        release_gates.reset_client_contract(token)
    assert result["status"] == "needs_clarification" and result["reason"] == "calories_only"
    assert fake.insert_count == 0
    assert "zero" in result["question"]


@pytest.mark.asyncio
async def test_gate_closing_mid_write_rolls_back_and_asks_without_uncertainty(monkeypatch):
    from store import CalorieOnlyWriteBlocked
    srv = _import_server(monkeypatch)
    fake = FakeVoiceStore()

    async def ready():
        return True

    async def blocked(*_args, **_kwargs):
        raise CalorieOnlyWriteBlocked("closed")
    fake.nullable_nutrients_ready = ready
    fake.insert_meal_idempotent = blocked
    monkeypatch.setattr(srv, "_client", fake)
    monkeypatch.setenv("CALORIE_ONLY_WRITES_ENABLED", "true")
    srv._voice_uncertain_meals.clear()
    token = _with_contract("nullable-v1")
    try:
        result = await _voice_calorie_only(srv, fake)
    finally:
        release_gates.reset_client_contract(token)
    assert result["status"] == "needs_clarification"
    assert not srv._voice_uncertain_meals, "a refused write is not an unknown outcome"


def test_backfill_never_selects_unknown_nutrient_rows():
    source = (SERVER / "scripts" / "backfill_food_catalog.py").read_text()
    for key in NUTRIENTS:
        assert f"{key} IS NOT NULL" in source


# --------------------------------------------------------------------------- #
# Migration hold (unit)
# --------------------------------------------------------------------------- #

def test_approval_parsing_requires_exact_sha256():
    digest = "a" * 64
    assert migrations.approved_migrations(f"{M003}:{digest}") == {M003: digest}
    assert migrations.approved_migrations(f"{M003}:{digest.upper()}") == {M003: digest}
    for bad in ("", M003, f"{M003}:", f"{M003}:abc", f"{M003}:{'g' * 64}", f":{digest}"):
        assert migrations.approved_migrations(bad) == {}


def test_real_003_file_requires_approval_of_its_exact_bytes():
    path = migrations.MIGRATIONS_DIR / M003
    digest = migrations.checksum(path)
    assert migrations.requires_hold(M003, path, {}) is True
    assert migrations.requires_hold(M003, path, {M003: "0" * 64}) is True
    assert migrations.requires_hold(M003, path, {M003: digest}) is False
    assert migrations.requires_hold("002_daily_workout_plans.sql",
                                    migrations.MIGRATIONS_DIR / "002_daily_workout_plans.sql", {}) is False


@pytest.mark.asyncio
async def test_status_tolerates_only_a_held_tail_and_fails_closed_behind_it(monkeypatch, tmp_path):
    files = []
    for name in ("001_a.sql", "002_gated.sql", "003_b.sql"):
        (tmp_path / name).write_text(f"-- {name}\n")
        files.append((name, tmp_path / name))
    monkeypatch.setattr(migrations, "APPROVAL_REQUIRED", frozenset({"002_gated.sql"}))

    class Conn:
        def __init__(self, applied):
            self.applied = applied
        async def fetchval(self, _sql):
            return True
        async def fetch(self, _sql):
            return [{"filename": n, "checksum": migrations.checksum(tmp_path / n)} for n in self.applied]

    monkeypatch.setattr(migrations, "migration_files", lambda: files[:2])
    status = await migrations.migration_status(Conn(["001_a.sql"]))
    assert status["compatible"] is True and status["held"] == ["002_gated.sql"]
    monkeypatch.setattr(migrations, "migration_files", lambda: files)
    status = await migrations.migration_status(Conn(["001_a.sql"]))
    assert status["compatible"] is False and status["held"] == []
    assert status["pending"] == ["002_gated.sql", "003_b.sql"]


# --------------------------------------------------------------------------- #
# Real PostgreSQL (throwaway local server only)
# --------------------------------------------------------------------------- #

pg = pytest.mark.skipif(not ADMIN_URL, reason="set MMMACROS_TEST_PG_ADMIN_URL to a throwaway local PostgreSQL")


def _db_url(name: str) -> str:
    base, _, query = ADMIN_URL.partition("?")
    root = base.rsplit("/", 1)[0]
    return f"{root}/{name}" + (f"?{query}" if query else "")


@pytest.fixture
async def scratch_db():
    import asyncpg
    name = "mmm_safeguards_" + uuid4().hex[:12]
    admin = await asyncpg.connect(ADMIN_URL)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    try:
        yield _db_url(name)
    finally:
        admin = await asyncpg.connect(ADMIN_URL)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


async def _nullable(conn) -> list[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name='nutrition_entries' "
        "AND column_name = ANY($1::text[]) AND is_nullable='YES' ORDER BY 1", list(NUTRIENTS))
    return [row["column_name"] for row in rows]


async def _ledger(conn) -> dict[str, str]:
    return {r["filename"]: r["checksum"] for r in await conn.fetch("SELECT filename, checksum FROM schema_migrations")}


async def _user(conn) -> object:
    user_id = uuid4()
    await conn.execute("INSERT INTO users(id, display_name) VALUES($1, 'Synthetic test user')", user_id)
    return user_id


def _approve_003(monkeypatch):
    digest = migrations.checksum(migrations.MIGRATIONS_DIR / M003)
    monkeypatch.setenv("APPROVED_MIGRATIONS", f"{M003}:{digest}")
    return digest


@pg
@pytest.mark.asyncio
async def test_pg_runner_holds_003_without_exact_approval_and_service_stays_compatible(scratch_db, monkeypatch):
    import asyncpg
    from store import Store
    held: list[str] = []
    applied = await migrations.apply_migrations(scratch_db, held)
    assert applied == ["000_legacy_schema.sql", "001_food_catalog.sql", "002_daily_workout_plans.sql"]
    assert held == [M003]
    monkeypatch.setenv("APPROVED_MIGRATIONS", f"{M003}:{'0' * 64}")  # stale/wrong approval
    held = []
    assert await migrations.apply_migrations(scratch_db, held) == [] and held == [M003]
    conn = await asyncpg.connect(scratch_db)
    try:
        assert await _nullable(conn) == []
        status = await migrations.migration_status(conn)
        assert status["compatible"] is True and status["held"] == [M003]
    finally:
        await conn.close()
    store = Store(scratch_db)
    try:
        await store.connect()  # held tail does not block startup
        assert await store.nullable_nutrients_ready() is False
    finally:
        await store.aclose()


@pg
@pytest.mark.asyncio
async def test_pg_write_gate_blocks_until_schema_switch_and_client_all_allow(scratch_db, monkeypatch):
    import asyncpg
    from store import CalorieOnlyWriteBlocked, Store
    await migrations.apply_migrations(scratch_db)
    conn = await asyncpg.connect(scratch_db)
    user_id = await _user(conn)
    store = Store(scratch_db)
    scope = bind_user(user_id)
    token = _with_contract("nullable-v1")
    values = dict(name="Calorie-only snack", meal="Snack", calories=200, protein=None, carbs=None,
                  fat=None, fiber=None, day=date(2026, 10, 7),
                  macro_source="User supplied calories; other nutrients unknown")
    try:
        monkeypatch.setenv("CALORIE_ONLY_WRITES_ENABLED", "true")
        with pytest.raises(CalorieOnlyWriteBlocked):  # 003 held: schema not ready
            await store.insert_meal(**values)
        _approve_003(monkeypatch)
        assert await migrations.apply_migrations(scratch_db) == [M003]
        assert await _nullable(conn) == sorted(NUTRIENTS)
        monkeypatch.setenv("CALORIE_ONLY_WRITES_ENABLED", "false")
        with pytest.raises(CalorieOnlyWriteBlocked):  # forward-disabled
            await store.insert_meal(**values)
        release_gates.reset_client_contract(token)
        token = _with_contract(None)
        monkeypatch.setenv("CALORIE_ONLY_WRITES_ENABLED", "true")
        with pytest.raises(CalorieOnlyWriteBlocked):  # old client
            await store.insert_meal(**values)
        assert await conn.fetchval("SELECT count(*) FROM nutrition_entries") == 0
        release_gates.reset_client_contract(token)
        token = _with_contract("nullable-v1")
        stored = await store.insert_meal(**values)
        assert all(stored[key] is None for key in NUTRIENTS) and stored["calories"] == 200
        row = await conn.fetchrow("SELECT protein, carbs, fat, fiber, food_item_id FROM nutrition_entries")
        assert all(row[key] is None for key in NUTRIENTS), "unknown must stay NULL, never zero"
        assert row["food_item_id"] is None, "unknown nutrients are not catalog evidence"
        assert await conn.fetchval("SELECT count(*) FROM food_nutrition_observations") == 0
        complete = await store.insert_meal(**{**values, "name": "Apple", "protein": 0.5, "carbs": 25,
                                              "fat": 0.3, "fiber": 4, "macro_source": "USDA FoodData Central"})
        assert complete["protein"] == 0.5
        backfill = load_script("backfill_food_catalog")
        pending = await backfill.pending_rows(conn)
        assert [r["name"] for _, r in pending] == [], "backfill must skip NULL rows"
    finally:
        release_gates.reset_client_contract(token)
        reset_user(scope)
        await store.aclose()
        await conn.close()


@pg
@pytest.mark.asyncio
async def test_pg_rollback_refuses_with_unknown_rows_and_preserves_values_and_history(scratch_db, monkeypatch):
    import asyncpg
    rollback = load_script("nutrient_rollback")
    _approve_003(monkeypatch)
    await migrations.apply_migrations(scratch_db)
    conn = await asyncpg.connect(scratch_db)
    try:
        user_id = await _user(conn)
        await conn.execute(
            "INSERT INTO nutrition_entries(id,user_id,name,meal,calories,protein,carbs,fat,fiber,day) "
            "VALUES('synthetic-null',$1,'Calorie-only snack','Snack',200,NULL,NULL,NULL,NULL,'2026-10-07')", user_id)
        ledger_before = await _ledger(conn)
        report = await rollback.preflight(conn)
        assert report["unknown_nutrient_rows"] == 1
        assert report["migration_003_applied"] is True
        assert "forward-disable only" in report["advice"]
        digest = rollback.rollback_checksum()
        with pytest.raises(rollback.RollbackRefused, match="checksum"):
            await rollback.restore_not_null(conn, "0" * 64)
        monkeypatch.setenv("CALORIE_ONLY_WRITES_ENABLED", "true")
        with pytest.raises(rollback.RollbackRefused, match="forward-disable first"):
            await rollback.restore_not_null(conn, digest)
        monkeypatch.setenv("CALORIE_ONLY_WRITES_ENABLED", "false")
        with pytest.raises(rollback.RollbackRefused, match="unknown nutrients"):
            await rollback.restore_not_null(conn, digest)
        # Nothing changed: still nullable, still NULL (never zero), history intact.
        assert await _nullable(conn) == sorted(NUTRIENTS)
        row = await conn.fetchrow("SELECT protein, carbs, fat, fiber FROM nutrition_entries WHERE id='synthetic-null'")
        assert all(row[key] is None for key in NUTRIENTS)
        assert await _ledger(conn) == ledger_before
        assert await conn.fetchval("SELECT to_regclass('public.schema_rollbacks') IS NULL")
    finally:
        await conn.close()


@pg
@pytest.mark.asyncio
async def test_pg_rollback_restores_not_null_only_when_empty_and_keeps_ledger(scratch_db, monkeypatch):
    import asyncpg
    from store import CalorieOnlyWriteBlocked, Store
    rollback = load_script("nutrient_rollback")
    digest_003 = _approve_003(monkeypatch)
    await migrations.apply_migrations(scratch_db)
    conn = await asyncpg.connect(scratch_db)
    store = Store(scratch_db)
    try:
        user_id = await _user(conn)
        ledger_before = await _ledger(conn)
        assert ledger_before[M003] == digest_003
        report = await rollback.restore_not_null(conn, rollback.rollback_checksum())
        assert report["nullable_nutrient_columns"] == []
        assert await _ledger(conn) == ledger_before, "schema_migrations is never edited"
        recorded = await conn.fetch("SELECT name, checksum FROM schema_rollbacks")
        assert [(r["name"], r["checksum"]) for r in recorded] == [
            ("003_restore_nutrients_not_null.sql", rollback.rollback_checksum())]
        assert await migrations.apply_migrations(scratch_db) == [], "003 is not re-applied"
        status = await migrations.migration_status(conn)
        assert status["compatible"] is True and status["pending"] == []
        # The live-schema check closes calorie-only writes even with every flag on.
        assert await store.nullable_nutrients_ready() is False
        monkeypatch.setenv("CALORIE_ONLY_WRITES_ENABLED", "true")
        scope, token = bind_user(user_id), _with_contract("nullable-v1")
        try:
            with pytest.raises(CalorieOnlyWriteBlocked):
                await store.insert_meal(name="x", meal="Snack", calories=100, protein=None, carbs=None,
                                        fat=None, fiber=None, day=date(2026, 10, 7), macro_source="User supplied")
        finally:
            release_gates.reset_client_contract(token)
            reset_user(scope)
        with pytest.raises(asyncpg.NotNullViolationError):  # the database itself enforces it now
            await conn.execute(
                "INSERT INTO nutrition_entries(id,user_id,name,meal,calories,protein,carbs,fat,fiber,day) "
                "VALUES('n',$1,'x','Snack',1,NULL,1,1,1,'2026-10-07')", user_id)
    finally:
        await store.aclose()
        await conn.close()
