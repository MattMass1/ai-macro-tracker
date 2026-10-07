"""Date-scoped (today-only) workout plans, outlook reads and honest calorie-only logging.

All data is synthetic, in-memory and tenant-bound. No database or provider is used.
"""
from copy import deepcopy
from uuid import uuid4

import pytest

import domain
from agent_canvas import CANVAS_INSTRUCTIONS, CanvasService
from auth import bind_user, current_user_id, reset_user
from test_agent_canvas import MemoryStore
from test_coach import FakeStore, rotation_plan
import server as srv


class DayPlanMixin:
    """Tenant-partitioned day-plan double with revision CAS + operation replay."""

    def _init_day_plans(self):
        self.day_plans = {}
        self.day_plan_writes = []

    async def fetch_day_workout_plan(self, day):
        row = self.day_plans.get((current_user_id(), day))
        return deepcopy(row) if row else None

    async def compare_and_swap_day_workout_plan(self, day, expected_revision, workout_type,
                                                exercises, operation_id, *, expected_context=None):
        if expected_context is not None and await self.fetch_workout_plan_context(day) != expected_context:
            return None
        key = (current_user_id(), day)
        current = self.day_plans.get(key)
        if (current is None) != (expected_revision is None):
            return None
        if current is not None and current["revision"] != expected_revision:
            return None
        row = {"day": day.isoformat(), "type": workout_type, "exercises": deepcopy(exercises),
               "revision": 1 if current is None else current["revision"] + 1,
               "operation_id": operation_id}
        self.day_plans[key] = row
        self.day_plan_writes.append(deepcopy(row))
        return deepcopy(row)


class CanvasDayStore(DayPlanMixin, MemoryStore):
    def __init__(self, plan=None):
        super().__init__()
        self._init_day_plans()
        self.plan = deepcopy(plan) if plan is not None else rotation_plan()
        self.plan_writes = []
        self.workouts = []

    async def fetch_workout_plan_context(self, day):
        return deepcopy({"routine": self.plan, "state": getattr(self, "state", None),
                         "daily": await self.fetch_day_workout_plan(day), "workouts": self.workouts})

    async def fetch_workout_plan(self):
        return deepcopy(self.plan)

    async def compare_and_swap_workout_plan(self, before, after):
        self.plan_writes.append(after)
        return None

    async def fetch_workout_library(self):
        return [{"name": n} for n in ("Bench Press", "Incline Dumbbell Press", "Lat Pulldown",
                                      "Barbell Row", "Face Pull", "Squat", "Leg Press")]

    async def fetch_workouts(self, start=None, end=None, exercise=None):
        return deepcopy(self.workouts)


class ServerDayStore(DayPlanMixin, FakeStore):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._init_day_plans()


def _service(store, session=None):
    async def today_session(_args):
        plan = await store.fetch_workout_plan()
        day = await store.fetch_day_workout_plan(domain.effective_date())
        if day:
            return {"today_type": day["type"], "exercises": day["exercises"], "done": False,
                    "has_plan": True, "today_source": "today_plan"}
        if not plan:
            return {"today_type": None, "exercises": [], "done": False, "has_plan": False}
        return {"today_type": "Push", "exercises": plan["days"]["Push"]["exercises"],
                "done": False, "has_plan": True}
    coach = {"get_today_session": session or today_session}
    return CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                         coach_factory=lambda: coach)


PULL = {"workout_type": "Pull", "exercises": [
    {"name": "lat pulldown", "sets": 3, "reps": "10-12"},
    {"name": "Barbell Row", "sets": 3, "reps": "8"},
    {"name": "Face Pull", "sets": 2, "reps": "15"},
]}


@pytest.mark.asyncio
async def test_today_proposal_previews_exactly_and_cancel_writes_nothing():
    store = CanvasDayStore()
    service = _service(store)
    sid = str(uuid4())
    scope = bind_user(uuid4())
    try:
        session = service.session(sid, create=True)
        result = await service._propose_today_workout(session, deepcopy(PULL))
        assert result["status"] == "awaiting_confirmation"
        today = domain.effective_date().isoformat()
        snap = await service.snapshot(sid)
        approval = snap["approval"]
        assert approval["status"] == "pending"
        assert today in approval["detail"] and "Today only" in approval["detail"]
        assert "saved routine is unchanged" in approval["detail"]
        surface = snap["surfaces"][0]
        assert surface["lifecycle"] == "approval"
        kinds = [c["component"] for c in surface["components"]]
        assert kinds == ["WorkoutPlanPreview", "ConfirmationCard"]
        rows = surface["components"][0]["rows"]
        # Library casing is canonical; exact sets/reps are shown.
        assert rows[0] == {"label": "Pull: Lat Pulldown", "detail": "3 sets × 10-12"}
        assert len(rows) == 3
        assert store.day_plan_writes == [] and store.plan_writes == []
        await service.action(sid, {"action": "cancel", "reference": approval["id"]})
        assert store.day_plan_writes == [] and store.plan_writes == []
        assert (await service.snapshot(sid))["approval"] is None
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_today_confirm_commits_once_routine_untouched_and_duplicate_confirm_rejected():
    store = CanvasDayStore()
    routine = deepcopy(store.plan)
    service = _service(store)
    sid = str(uuid4())
    scope = bind_user(uuid4())
    try:
        session = service.session(sid, create=True)
        await service._propose_today_workout(session, deepcopy(PULL))
        draft = (await service.snapshot(sid))["approval"]["id"]
        response = await service.action(sid, {"action": "confirm", "reference": draft})
        assert response["approval"] is None
        assert len(store.day_plan_writes) == 1
        saved = store.day_plan_writes[0]
        assert saved["day"] == domain.effective_date().isoformat()
        assert saved["type"] == "Pull"
        assert [e["name"] for e in saved["exercises"]] == ["Lat Pulldown", "Barbell Row", "Face Pull"]
        # A plan is not training evidence: nothing records sets/weights as completed.
        assert all(set(e) <= {"name", "sets", "reps", "rest_sec"} for e in saved["exercises"])
        assert store.plan == routine and store.plan_writes == []
        # The existing logger flow shows the saved plan (type/exercise only).
        assert response["workout"]["type"] == "Pull"
        assert [e["name"] for e in response["workout"]["exercises"]][0] == "Lat Pulldown"
        assert response["workout"]["loggedSets"] == {}
        with pytest.raises(ValueError, match="no longer available"):
            await service.action(sid, {"action": "confirm", "reference": draft})
        assert len(store.day_plan_writes) == 1
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_today_stale_preview_never_overwrites_newer_today_plan():
    store = CanvasDayStore()
    service = _service(store)
    user = uuid4()
    scope = bind_user(user)
    sid = str(uuid4())
    try:
        session = service.session(sid, create=True)
        await service._propose_today_workout(session, deepcopy(PULL))
        draft = session.approval["id"]
        # Another device saves a different today plan after this preview.
        await store.compare_and_swap_day_workout_plan(
            domain.effective_date(), None, "Legs", [{"name": "Squat", "sets": 3, "reps": "5"}], "other-op")
        with pytest.raises(ValueError, match="changed"):
            await service.action(sid, {"action": "confirm", "reference": draft})
        current = await store.fetch_day_workout_plan(domain.effective_date())
        assert current["type"] == "Legs" and current["operation_id"] == "other-op"
        assert len(store.day_plan_writes) == 1
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_today_confirm_replay_after_lost_response_is_success_not_second_write():
    store = CanvasDayStore()
    service = _service(store)
    scope = bind_user(uuid4())
    sid = str(uuid4())
    try:
        session = service.session(sid, create=True)
        await service._propose_today_workout(session, deepcopy(PULL))
        draft = session.approval
        # The same operation already committed (e.g. duplicate event / lost response).
        await store.compare_and_swap_day_workout_plan(
            domain.effective_date(), None, draft["after"]["type"], draft["after"]["exercises"],
            draft["operation_id"])
        response = await service.action(sid, {"action": "confirm", "reference": draft["id"]})
        assert response["approval"] is None
        assert len(store.day_plan_writes) == 1
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_today_proposal_unknown_exercise_asks_and_stages_nothing():
    store = CanvasDayStore()
    service = _service(store)
    scope = bind_user(uuid4())
    try:
        session = service.session(str(uuid4()), create=True)
        result = await service._propose_today_workout(session, {
            "workout_type": "Pull", "exercises": [{"name": "Invented Row Machine 3000", "sets": 3, "reps": "10"}]})
        assert result["status"] == "needs_clarification"
        assert "Invented Row Machine 3000" in result["question"]
        assert session.approval is None and store.day_plan_writes == []
        with pytest.raises(ValueError):
            await service._propose_today_workout(session, {"workout_type": "Pull", "exercises": None})
        with pytest.raises(ValueError):
            await service._propose_today_workout(session, {"workout_type": "Pull", "exercises": [
                {"name": "Face Pull", "sets": 99, "reps": "10"}]})
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_today_edit_preserves_exercise_with_completed_sets():
    store = CanvasDayStore()
    today = domain.effective_date().isoformat()
    store.workouts = [{"id": "row-1", "exercise": "Bench Press", "workout_type": "Push",
                       "date": today, "sets": [{"weight": 135, "reps": 8}]}]
    service = _service(store)
    scope = bind_user(uuid4())
    sid = str(uuid4())
    try:
        session = service.session(sid, create=True)
        # "Swap bench for incline dumbbell press" after bench was already trained.
        result = await service._propose_today_workout(session, {"workout_type": "Push", "exercises": [
            {"name": "Incline Dumbbell Press", "sets": 3, "reps": "10"}]})
        assert result["status"] == "awaiting_confirmation"
        names = [e["name"] for e in session.approval["after"]["exercises"]]
        assert names == ["Bench Press", "Incline Dumbbell Press"]
        assert "Kept Bench Press" in session.approval["detail"]
        await service.action(sid, {"action": "confirm", "reference": session.approval["id"]})
        assert [e["name"] for e in store.day_plan_writes[0]["exercises"]] == names
        assert store.workouts[0]["sets"] == [{"weight": 135, "reps": 8}]
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_today_plan_is_tenant_scoped():
    store = CanvasDayStore()
    service = _service(store)
    owner, other = uuid4(), uuid4()
    scope = bind_user(owner)
    sid = str(uuid4())
    try:
        session = service.session(sid, create=True)
        await service._propose_today_workout(session, deepcopy(PULL))
        await service.action(sid, {"action": "confirm", "reference": session.approval["id"]})
    finally:
        reset_user(scope)
    scope = bind_user(other)
    try:
        assert await store.fetch_day_workout_plan(domain.effective_date()) is None
        with pytest.raises(Exception):
            service.session(sid)  # another tenant cannot reach the owner's canvas session
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_canvas_catalog_and_instructions_offer_today_tools_by_default():
    store = CanvasDayStore()
    seen = {}

    async def agent(**kwargs):
        seen["names"] = [tool["name"] for tool in kwargs["tool_catalog"]]
        seen["handlers"] = set(kwargs["handlers"])
        return "ok", []
    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                            coach_factory=lambda: {"get_today_session": _service(store).coach_factory()["get_today_session"],
                                                   "get_workout_outlook": lambda _a: None}, agent=agent)
    scope = bind_user(uuid4())
    try:
        await service.turn(str(uuid4()), str(uuid4()), "What's my workout today?", adapter="voice")
    finally:
        reset_user(scope)
    assert {"propose_today_workout", "get_workout_outlook"} <= set(seen["names"])
    assert {"propose_today_workout", "get_workout_outlook"} <= seen["handlers"]
    assert "propose_today_workout" in CANVAS_INSTRUCTIONS
    assert "today only" in CANVAS_INSTRUCTIONS.casefold()


@pytest.mark.asyncio
async def test_canvas_function_schemas_are_accepted_by_chat_completions():
    # Production regression: Chat Completions rejects (HTTP 400) any function whose
    # parameters carry anyOf/oneOf/allOf/enum/not at the top level, which failed
    # every canvas turn. Conditional requirements are enforced server-side instead.
    store = CanvasDayStore()
    seen = {}

    async def agent(**kwargs):
        seen["catalog"] = kwargs["tool_catalog"]
        return "ok", []
    service = CanvasService(store_factory=lambda: store, food_factory=lambda: {},
                            coach_factory=lambda: {"get_today_session": _service(store).coach_factory()["get_today_session"],
                                                   "get_workout_outlook": lambda _a: None}, agent=agent)
    scope = bind_user(uuid4())
    try:
        await service.turn(str(uuid4()), str(uuid4()), "Change my push day bench to 4 sets", adapter="voice")
    finally:
        reset_user(scope)
    assert seen["catalog"]
    for tool in seen["catalog"]:
        schema = tool["input_schema"]
        assert schema.get("type") == "object", tool["name"]
        assert not {"anyOf", "oneOf", "allOf", "enum", "not"} & set(schema), tool["name"]


# ── Server: day-plan overlay, outlook and SQL tenant scoping ─────────────

@pytest.mark.asyncio
async def test_today_plan_type_must_stay_loggable_by_existing_logger():
    store = CanvasDayStore()
    service = _service(store)
    scope = bind_user(uuid4())
    try:
        session = service.session(str(uuid4()), create=True)
        result = await service._propose_today_workout(session, {
            "workout_type": "Upper Body", "exercises": [{"name": "Face Pull", "sets": 2, "reps": "15"}]})
        assert result["status"] == "needs_clarification" and "Push" in result["question"]
        assert session.approval is None
        result = await service._propose_today_workout(session, {
            "workout_type": "pull", "exercises": [{"name": "Face Pull", "sets": 2, "reps": "15"}]})
        assert session.approval["after"]["type"] == "Pull"
        assert domain.normalize_workout_type(session.approval["after"]["type"]) == "Pull"
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_lost_confirm_response_reconciles_read_only_on_refresh():
    store = CanvasDayStore()
    original = store.compare_and_swap_day_workout_plan

    async def stored_then_lost(*args, **kwargs):
        await original(*args, **kwargs)
        raise ConnectionError("response lost after commit")
    store.compare_and_swap_day_workout_plan = stored_then_lost
    service = _service(store)
    scope = bind_user(uuid4())
    sid = str(uuid4())
    try:
        session = service.session(sid, create=True)
        await service._propose_today_workout(session, deepcopy(PULL))
        draft = session.approval["id"]
        with pytest.raises(ConnectionError):
            await service.action(sid, {"action": "confirm", "reference": draft})
        assert (await service.snapshot(sid))["approval"]["status"] == "uncertain"
        response = await service.action(sid, {"action": "refresh"})
        assert response["approval"] is None
        assert response["workout"]["type"] == "Pull"
        assert len(store.day_plan_writes) == 1
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_confirm_refuses_when_dropped_exercise_was_trained_after_preview():
    store = CanvasDayStore()
    service = _service(store)
    scope = bind_user(uuid4())
    sid = str(uuid4())
    try:
        session = service.session(sid, create=True)
        await service._propose_today_workout(session, {"workout_type": "Push", "exercises": [
            {"name": "Incline Dumbbell Press", "sets": 3, "reps": "10"}]})
        store.workouts = [{"id": "r9", "exercise": "Bench Press", "workout_type": "Push",
                           "date": domain.effective_date().isoformat(), "sets": [{"weight": 95, "reps": 10}]}]
        with pytest.raises(ValueError, match="fresh preview"):
            await service.action(sid, {"action": "confirm", "reference": session.approval["id"]})
        assert store.day_plan_writes == []
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_contextual_swap_never_edits_routine_on_today_plan_day():
    store = CanvasDayStore()
    service = _service(store)
    scope = bind_user(uuid4())
    sid = str(uuid4())
    try:
        session = service.session(sid, create=True)
        await service._propose_today_workout(session, deepcopy(PULL))
        await service.action(sid, {"action": "confirm", "reference": session.approval["id"]})
        result = await service._request_swap(session, {"target": "last", "replacement": "Squat"})
        assert result["status"] == "needs_clarification"
        assert session.approval is None and store.plan_writes == []
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_routine_less_completion_does_not_skip_future_routine_start(monkeypatch):
    fake = ServerDayStore(plan=None)
    monkeypatch.setattr(srv, "_client", fake)
    scope = bind_user(uuid4())
    try:
        await fake.compare_and_swap_day_workout_plan(
            domain.effective_date(), None, "Pull", [{"name": "Lat Pulldown", "sets": 3, "reps": "10"}], "op-1")
        done = await srv._coach_tool_handlers()["complete_today_session"]({})
        assert done["done"] is True and done["today_type"] == "Pull"
        assert fake.session_state["rotation_index"] == -1
        payload = await srv.workout_plan_payload()
        assert payload["has_plan"] is True  # a confirmed today plan unlocks the logger
        assert payload["upcoming"][0]["today_plan"] is True
    finally:
        reset_user(scope)
    fake.plan = rotation_plan()
    fake.session_state["done_date"] = domain.effective_date() - __import__("datetime").timedelta(days=1)
    scope = bind_user(uuid4())
    try:
        index, today_type, _exercises, _done = await srv._resolve_today_session()
    finally:
        reset_user(scope)
    assert (index, today_type) == (0, "Push")

FIXTURE_SESSION = "00000000-0000-4000-8000-000000000001"
FIXTURE_DRAFT = "00000000-0000-4000-8000-000000000003"


async def serialize_day_plan_fixture():
    """The exact envelope the native app receives for a today-only preview."""
    import agent_canvas
    from datetime import date
    original = agent_canvas.effective_date
    agent_canvas.effective_date = lambda: date(2026, 10, 7)
    store = CanvasDayStore()
    service = _service(store)
    scope = bind_user(uuid4())
    try:
        session = service.session(FIXTURE_SESSION, create=True)
        await service._propose_today_workout(session, deepcopy(PULL))
        actual = await service.snapshot(FIXTURE_SESSION)
    finally:
        reset_user(scope)
        agent_canvas.effective_date = original
    draft_id = actual["approval"]["id"]
    actual["instanceId"] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    actual["serverTime"] = 1000
    actual["approval"]["id"] = FIXTURE_DRAFT
    for component in actual["surfaces"][0]["components"]:
        if component.get("reference") == draft_id:
            component["reference"] = FIXTURE_DRAFT
        for action in component.get("actions", []):
            if action.get("reference") == draft_id:
                action["reference"] = FIXTURE_DRAFT
    return actual


@pytest.mark.asyncio
async def test_python_day_plan_serializer_matches_shared_native_fixture():
    import json
    from pathlib import Path
    fixture = json.loads((Path(__file__).resolve().parents[2]
                          / "docs/agent-canvas/fixtures/day-plan.json").read_text())
    assert await serialize_day_plan_fixture() == fixture
    assert fixture["approval"]["title"] == "Save today's workout?"
    assert "commit" not in json.dumps(fixture)


@pytest.mark.asyncio
async def test_today_session_and_payload_overlay_saved_today_plan(monkeypatch):
    fake = ServerDayStore(plan=rotation_plan())
    monkeypatch.setattr(srv, "_client", fake)
    scope = bind_user(uuid4())
    try:
        await fake.compare_and_swap_day_workout_plan(
            domain.effective_date(), None, "Pull",
            [{"name": "Face Pull", "sets": 2, "reps": "15", "rest_sec": 60}], "op-1")
        session = await srv._coach_tool_handlers()["get_today_session"]({})
        assert session["today_type"] == "Pull"
        assert session["exercises"] == [{"name": "Face Pull", "sets": 2, "reps": "15", "rest_sec": 60}]
        assert session["today_source"] == "today_plan"
        payload = await srv.workout_plan_payload()
        assert payload["upcoming"][0]["type"] == "Pull"
        assert payload["upcoming"][0]["exercises"] == [{"name": "Face Pull"}]
        assert payload["upcoming"][0]["today_plan"] is True
        # Upcoming days still come from the stored routine.
        assert payload["upcoming"][1]["exercises"]
        assert fake.plan == rotation_plan()
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_today_plan_without_routine_is_loggable(monkeypatch):
    fake = ServerDayStore(plan=None)
    monkeypatch.setattr(srv, "_client", fake)
    scope = bind_user(uuid4())
    try:
        await fake.compare_and_swap_day_workout_plan(
            domain.effective_date(), None, "Pull", [{"name": "Lat Pulldown", "sets": 3, "reps": "10"}], "op-1")
        session = await srv._coach_tool_handlers()["get_today_session"]({})
        assert session["has_plan"] is True and session["today_type"] == "Pull"
        payload = await srv.workout_plan_payload()
        assert payload["upcoming"][0]["exercises"] == [{"name": "Lat Pulldown"}]
        assert fake.plan is None
    finally:
        reset_user(scope)


@pytest.mark.asyncio
async def test_workout_outlook_labels_plan_vs_completed_and_upcoming(monkeypatch):
    fake = ServerDayStore(plan=rotation_plan())
    today = domain.effective_date().isoformat()
    fake.workouts = [{"id": "r1", "exercise": "Bench Press", "workout_type": ["Push"],
                      "date": today, "sets": [{"weight": 135, "reps": 8}, {"weight": 135, "reps": 7}]}]
    monkeypatch.setattr(srv, "_client", fake)
    scope = bind_user(uuid4())
    try:
        outlook = await srv._coach_tool_handlers(canvas=True)["get_workout_outlook"]({})
    finally:
        reset_user(scope)
    assert outlook["date"] == today
    assert outlook["today"]["source"] == "routine"
    assert outlook["today"]["planned"][0]["name"] == "Bench Press"
    assert outlook["today"]["completed"] == [{"exercise": "Bench Press", "sets_logged": 2}]
    assert outlook["today"]["planned_is_not_completed"] is True
    assert [day["type"] for day in outlook["upcoming"]][:2] == ["Pull", "Legs"]


@pytest.mark.asyncio
async def test_day_plan_sql_is_user_scoped_and_revision_guarded():
    from store import Store
    captured = []

    class Pool:
        def acquire(self): return self
        def transaction(self): return self
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def execute(self, sql, *args):
            assert "pg_advisory_xact_lock" in sql
            assert args == (str(user),)
        async def fetchrow(self, sql, *args):
            captured.append((sql, args))
            return None

    store = Store("postgresql://fixture.invalid/not-used")

    async def connect():
        return Pool()
    store.connect = connect
    user = uuid4()
    scope = bind_user(user)
    try:
        day = domain.effective_date()
        assert await store.fetch_day_workout_plan(day) is None
        assert await store.compare_and_swap_day_workout_plan(day, None, "Pull", [], "op") is None
        assert await store.compare_and_swap_day_workout_plan(day, 3, "Pull", [], "op") is None
    finally:
        reset_user(scope)
    read, insert, update = captured
    assert "WHERE user_id=$1 AND day=$2" in read[0] and read[1][0] == user
    assert "ON CONFLICT (user_id, day) DO NOTHING" in insert[0] and insert[1][0] == user
    assert "revision=$3" in update[0] and "revision=revision+1" in update[0] and update[1][0] == user


def test_migration_adds_only_the_day_plan_table():
    from pathlib import Path
    sql = (Path(srv.__file__).resolve().parents[1] / "migrations/002_daily_workout_plans.sql").read_text()
    assert "CREATE TABLE daily_workout_plans" in sql
    assert "PRIMARY KEY (user_id, day)" in sql
    for forbidden in ("DROP ", "DELETE ", "UPDATE ", "ALTER TABLE meals", "TRUNCATE"):
        assert forbidden not in sql.upper()


# ── Honest calorie-only logging ──────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("description", [
    "a 200-calorie snack", "200 calorie snack", "300 kcal", "150 cals of candy",
    "about 200 calories", "250 calories worth of snack",
])
async def test_voice_calorie_only_request_never_writes_zero_macros(monkeypatch, description):
    from test_live_coach import FakeVoiceStore
    fake = FakeVoiceStore()
    monkeypatch.setattr(srv, "_client", fake)

    async def no_lookup(_query, **_kwargs):
        raise AssertionError("calorie-only text must not be resolved as a food")
    monkeypatch.setattr(srv, "resolve_food", no_lookup)
    scope = bind_user(uuid4())
    try:
        result = await srv._voice_tool_handlers()["log_meal"]("cal-only", {
            "description": description, "meal_type": "Snack"})
    finally:
        reset_user(scope)
    assert fake.insert_count == 1
    assert result["status"] == "committed"
    assert all(result["logged"][key] is None for key in ("protein", "carbs", "fat", "fiber"))
    assert "unknown" in result["confirmation"]
    assert "0g protein" not in result["confirmation"]
