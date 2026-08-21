"""Typed, tenant-scoped PostgreSQL store for the macro tracker."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping
from uuid import UUID, uuid4

import asyncpg

from auth import current_user_id
from domain import effective_day_window, validate_macro_source


class StoreError(RuntimeError):
    pass


class InviteNotFound(StoreError):
    pass


class InviteAlreadyClaimed(StoreError):
    pass


class ChatQuotaExceeded(StoreError):
    """The user's daily chat message quota is exhausted."""


def hash_device_token(token: str) -> str:
    """Return the one-way identifier persisted for a raw device token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _dict(row: asyncpg.Record | None) -> dict[str, Any] | None:
    return None if row is None else {key: _value(value) for key, value in row.items()}


def meal(row: asyncpg.Record) -> dict[str, Any]:
    data = _dict(row) or {}
    return {"id": data["id"], "name": data["name"], "meal": data["meal"],
            **{k: data[k] for k in ("calories", "protein", "carbs", "fat", "fiber")},
            "date": data["day"], "created_time": data["created_at"]}


def workout(row: asyncpg.Record) -> dict[str, Any]:
    data = _dict(row) or {}; sets = []
    for i in range(1, 5):
        weight, reps = data.get(f"weight_{i}"), data.get(f"reps_{i}")
        if weight or reps: sets.append({"weight": weight or 0.0, "reps": reps or 0.0})
    return {"id": data["id"], "exercise": data["exercise_name"],
            "workout_type": data["workout_type"], "muscle_group": data["muscle_group"],
            "sets": sets, "date": data.get("day") or data["created_at"][:10],
            "created_time": data["created_at"]}


class Store:
    def __init__(self, database_url: str):
        if not database_url: raise ValueError("DATABASE_URL is required")
        self.database_url, self.pool = database_url, None

    async def connect(self) -> asyncpg.Pool:
        if self.pool is None:
            try: self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=10)
            except asyncpg.PostgresError as exc: raise StoreError(str(exc)) from exc
        return self.pool

    async def aclose(self) -> None:
        if self.pool is not None: await self.pool.close(); self.pool = None

    async def resolve_device(self, raw_token: str) -> UUID | None:
        pool = await self.connect()
        return await pool.fetchval(
            "UPDATE devices SET last_seen=now() WHERE token_hash=$1 RETURNING user_id",
            hash_device_token(raw_token),
        )

    async def claim_invite(self, code: str, label: str | None = None, display_name: str | None = None) -> dict[str, str]:
        pool = await self.connect()
        async with pool.acquire() as conn, conn.transaction():
            invite = await conn.fetchrow(
                "SELECT i.user_id,i.claimed_at,u.display_name FROM invite_codes i "
                "JOIN users u ON u.id=i.user_id WHERE i.code=$1 FOR UPDATE OF i", code
            )
            if invite is None:
                raise InviteNotFound(code)
            if invite["claimed_at"] is not None:
                raise InviteAlreadyClaimed(code)
            raw_token = secrets.token_urlsafe(32)
            await conn.execute(
                "INSERT INTO devices(token_hash,user_id,label) VALUES($1,$2,$3)",
                hash_device_token(raw_token), invite["user_id"], label,
            )
            await conn.execute("UPDATE invite_codes SET claimed_at=now() WHERE code=$1", code)
            name = display_name
            if name:
                name = name.strip()[:40]
                if name:
                    await conn.execute(
                        "UPDATE users SET display_name=$1 WHERE id=$2", name, invite["user_id"]
                    )
            return {"token": raw_token, "display_name": name or invite["display_name"]}

    async def fetch_meals(self, start: date, end: date | None = None):
        pool = await self.connect(); end = end or start; user_id = current_user_id()
        return [meal(r) for r in await pool.fetch(
            "SELECT * FROM nutrition_entries WHERE user_id=$1 AND day BETWEEN $2 AND $3 "
            "ORDER BY day, created_at DESC", user_id, start, end)]

    async def fetch_day_rollups(self, start: date | None = None, end: date | None = None):
        pool = await self.connect(); user_id = current_user_id()
        clauses = ["user_id=$1"]; args: list[Any] = [user_id]
        if start is not None: args.append(start); clauses.append(f"date >= ${len(args)}")
        if end is not None: args.append(end); clauses.append(f"date <= ${len(args)}")
        rows = await pool.fetch("SELECT * FROM days WHERE " + " AND ".join(clauses) + " ORDER BY date", *args)
        return [_dict(row) for row in rows]

    async def fetch_meal_rollups(self, day: date):
        pool = await self.connect(); user_id = current_user_id()
        rows = await pool.fetch(
            "SELECT * FROM meals WHERE user_id=$1 AND day=$2 ORDER BY "
            "CASE meal_type WHEN 'Breakfast' THEN 1 WHEN 'Lunch' THEN 2 "
            "WHEN 'Dinner' THEN 3 WHEN 'Snack' THEN 4 ELSE 5 END, meal_type", user_id, day)
        return [_dict(row) for row in rows]

    async def fetch_targets(self, day: date):
        pool = await self.connect(); user_id = current_user_id()
        return _dict(await pool.fetchrow(
            "SELECT * FROM macro_targets WHERE user_id=$1 AND effective_date <= $2 "
            "ORDER BY effective_date DESC, created_at DESC LIMIT 1", user_id, day))

    async def fetch_presets(self, active_only=True):
        pool = await self.connect(); user_id = current_user_id()
        active = " AND active" if active_only else ""
        return [_dict(r) for r in await pool.fetch(
            f"SELECT * FROM meal_presets WHERE user_id=$1{active} ORDER BY sort_order, lower(name)", user_id)]

    async def fetch_workouts(self, start: date | None = None, end: date | None = None, exercise: str | None = None):
        pool = await self.connect(); args: list[Any] = [current_user_id()]; clauses=["user_id=$1"]
        if start is not None: args.append(start); clauses.append(f"day >= ${len(args)}")
        if end is not None: args.append(end); clauses.append(f"day <= ${len(args)}")
        if exercise is not None: args.append(exercise); clauses.append(f"exercise_name = ${len(args)}")
        rows = await pool.fetch("SELECT * FROM fitness_tracker WHERE " + " AND ".join(clauses) + " ORDER BY day DESC NULLS LAST, created_at DESC", *args)
        return [workout(r) for r in rows]

    async def fetch_prs(self):
        pool=await self.connect(); return [_dict(r) for r in await pool.fetch(
            "SELECT * FROM exercise_max_reps WHERE user_id=$1 ORDER BY date_achieved DESC NULLS LAST", current_user_id())]

    async def fetch_workout_plan(self) -> dict[str, Any] | None:
        pool = await self.connect()
        plan = await pool.fetchval(
            "SELECT plan FROM workout_plans WHERE user_id=$1", current_user_id()
        )
        if plan is None:
            return None
        return json.loads(plan) if isinstance(plan, str) else dict(plan)

    async def put_workout_plan(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        """Upsert the authenticated user's validated workout plan."""
        pool = await self.connect()
        stored = await pool.fetchval(
            "INSERT INTO workout_plans(user_id,plan) VALUES($1,$2::jsonb) "
            "ON CONFLICT(user_id) DO UPDATE SET plan=EXCLUDED.plan,updated_at=now() "
            "RETURNING plan",
            current_user_id(), json.dumps(plan),
        )
        return json.loads(stored) if isinstance(stored, str) else dict(stored)

    async def put_display_name(self, name: str) -> dict[str, str]:
        """Update the authenticated user's display name."""
        pool = await self.connect()
        stored = await pool.fetchval(
            "UPDATE users SET display_name=$1 WHERE id=$2 RETURNING display_name",
            name, current_user_id(),
        )
        if stored is None:
            raise StoreError("Authenticated user was not found")
        return {"display_name": str(stored)}

    async def get_metrics(self) -> dict[str, Any] | None:
        """Return measurements for the authenticated user."""
        pool = await self.connect()
        return _dict(await pool.fetchrow(
            "SELECT height_cm,weight_kg,goal_weight_kg,age,activity_level,updated_at "
            "FROM user_metrics WHERE user_id=$1", current_user_id(),
        ))

    async def put_metrics(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Upsert measurements for the authenticated user."""
        pool = await self.connect()
        row = await pool.fetchrow(
            "INSERT INTO user_metrics(user_id,height_cm,weight_kg,goal_weight_kg,age,activity_level) "
            "VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(user_id) DO UPDATE SET "
            "height_cm=EXCLUDED.height_cm,weight_kg=EXCLUDED.weight_kg,"
            "goal_weight_kg=EXCLUDED.goal_weight_kg,age=EXCLUDED.age,"
            "activity_level=EXCLUDED.activity_level,updated_at=now() "
            "RETURNING height_cm,weight_kg,goal_weight_kg,age,activity_level,updated_at",
            current_user_id(), values["height_cm"], values["weight_kg"],
            values["goal_weight_kg"], values.get("age"), values.get("activity_level"),
        )
        return _dict(row) or {}

    async def fetch_workout_library(self) -> list[dict[str, Any]]:
        """Return the shared exercise library in stable type/name order."""
        pool = await self.connect()
        rows = await pool.fetch(
            "SELECT name,muscle_group,workout_type,equipment,difficulty,swaps,"
            "video_url,instructions "
            "FROM workout_library ORDER BY workout_type, lower(name)"
        )
        return [_dict(row) or {} for row in rows]

    async def insert_meal(self, *, name, meal, calories, protein, carbs, fat, fiber, day, macro_source):
        pool = await self.connect(); entry_id = str(uuid4()); user_id = current_user_id()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("INSERT INTO days(user_id,date) VALUES($1,$2) ON CONFLICT(user_id,date) DO NOTHING", user_id, day)
            await conn.fetchval("SELECT date FROM days WHERE user_id=$1 AND date=$2 FOR UPDATE", user_id, day)
            meal_id = await conn.fetchval(
                "INSERT INTO meals(id,user_id,day,meal_type) VALUES($1,$2,$3,$4) "
                "ON CONFLICT(user_id,day,meal_type) DO UPDATE SET meal_type=EXCLUDED.meal_type RETURNING id",
                str(uuid4()), user_id, day, meal)
            row = await conn.fetchrow(
                "INSERT INTO nutrition_entries(id,user_id,name,meal,calories,protein,carbs,fat,fiber,day,meal_id,macro_source) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING *",
                entry_id, user_id, name, meal, calories, protein, carbs, fat, fiber, day, meal_id, macro_source)
            await self._refresh_rollups(conn, user_id, day)
        return meal_row(row)

    @staticmethod
    async def _refresh_rollups(conn: asyncpg.Connection, user_id: UUID, day: date) -> None:
        await conn.fetchval("SELECT date FROM days WHERE user_id=$1 AND date=$2 FOR UPDATE", user_id, day)
        await conn.execute(
            "UPDATE nutrition_entries e SET meal_id=m.id FROM meals m WHERE m.user_id=$1 "
            "AND e.user_id=$1 AND m.day=$2 AND e.day=$2 AND e.meal=m.meal_type AND e.meal_id IS NULL", user_id, day)
        await conn.execute(
            "UPDATE meals m SET calories=x.calories,protein=x.protein,carbs=x.carbs,fat=x.fat,fiber=x.fiber FROM ("
            "SELECT m2.id,COALESCE(sum(e.calories),0) calories,COALESCE(sum(e.protein),0) protein,"
            "COALESCE(sum(e.carbs),0) carbs,COALESCE(sum(e.fat),0) fat,COALESCE(sum(e.fiber),0) fiber "
            "FROM meals m2 LEFT JOIN nutrition_entries e ON e.user_id=$1 AND e.meal_id=m2.id "
            "WHERE m2.user_id=$1 AND m2.day=$2 GROUP BY m2.id) x WHERE m.user_id=$1 AND m.id=x.id", user_id, day)
        await conn.execute(
            "INSERT INTO days(user_id,date,calories,protein,carbs,fat,fiber) SELECT $1,$2,"
            "COALESCE(sum(calories),0),COALESCE(sum(protein),0),COALESCE(sum(carbs),0),"
            "COALESCE(sum(fat),0),COALESCE(sum(fiber),0) FROM nutrition_entries WHERE user_id=$1 AND day=$2 "
            "ON CONFLICT(user_id,date) DO UPDATE SET calories=EXCLUDED.calories,protein=EXCLUDED.protein,"
            "carbs=EXCLUDED.carbs,fat=EXCLUDED.fat,fiber=EXCLUDED.fiber", user_id, day)

    async def insert_workout(self, *, exercise, workout_type, muscle_group, sets, day):
        pool=await self.connect(); id=str(uuid4()); vals=[]; user_id=current_user_id()
        for i in range(4): vals += [sets[i]["weight"], sets[i]["reps"]] if i < len(sets) else [None,None]
        r=await pool.fetchrow("INSERT INTO fitness_tracker(id,user_id,exercise_name,workout_type,muscle_group,weight_1,reps_1,weight_2,reps_2,weight_3,reps_3,weight_4,reps_4,day) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14) RETURNING *",id,user_id,exercise,workout_type,muscle_group,*vals,day); return workout(r)

    async def save_preset(self, values: Mapping[str, Any], existing_id: str | None = None):
        # Provenance choke point: no caller may persist a preset without citing
        # where its macros came from — a preset is a deferred meal log.
        macro_source = validate_macro_source(str(values.get('macro_source') or ''))
        pool=await self.connect(); id=existing_id or str(uuid4()); user_id=current_user_id()
        r=await pool.fetchrow("INSERT INTO meal_presets(id,user_id,name,emoji,calories,protein,carbs,fat,fiber,meal,sort_order,active,macro_source) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,true,$12) ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,emoji=EXCLUDED.emoji,calories=EXCLUDED.calories,protein=EXCLUDED.protein,carbs=EXCLUDED.carbs,fat=EXCLUDED.fat,fiber=EXCLUDED.fiber,meal=EXCLUDED.meal,active=true,macro_source=EXCLUDED.macro_source WHERE meal_presets.user_id=EXCLUDED.user_id RETURNING *",id,user_id,values['name'],values['emoji'],values['calories'],values['protein'],values['carbs'],values['fat'],values['fiber'],values['meal'],values.get('sort_order',0),macro_source);
        if r is None: raise StoreError("Preset does not belong to authenticated user")
        return _dict(r)

    async def insert_targets(self, day: date, values: Mapping[str, float]):
        pool=await self.connect(); id=str(uuid4()); user_id=current_user_id()
        r=await pool.fetchrow("INSERT INTO macro_targets(id,user_id,name,calories,protein,carbs,fat,fiber,effective_date) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *",id,user_id,f"Targets from {day.isoformat()}",values['calories'],values['protein'],values['carbs'],values['fat'],values['fiber'],day); return _dict(r)

    async def delete(self, table: str, id: str):
        if table not in {'nutrition_entries','fitness_tracker'}: raise ValueError('invalid table')
        pool = await self.connect(); user_id=current_user_id()
        if table == 'fitness_tracker':
            await pool.execute("DELETE FROM fitness_tracker WHERE user_id=$1 AND id=$2", user_id, id); return
        async with pool.acquire() as conn, conn.transaction():
            existing = await conn.fetchrow("SELECT day FROM nutrition_entries WHERE user_id=$1 AND id=$2", user_id, id)
            if existing is None: return
            await conn.fetchval("SELECT date FROM days WHERE user_id=$1 AND date=$2 FOR UPDATE", user_id, existing["day"])
            removed = await conn.fetchrow("DELETE FROM nutrition_entries WHERE user_id=$1 AND id=$2 RETURNING day", user_id, id)
            if removed is None: return
            await self._refresh_rollups(conn, user_id, removed["day"])
            await conn.execute("DELETE FROM meals WHERE user_id=$1 AND day=$2 AND NOT EXISTS (SELECT 1 FROM nutrition_entries WHERE user_id=$1 AND meal_id=meals.id)", user_id, removed["day"])
            await conn.execute("DELETE FROM days WHERE user_id=$1 AND date=$2 AND NOT EXISTS (SELECT 1 FROM nutrition_entries WHERE user_id=$1 AND day=$2)", user_id, removed["day"])

    async def get_brief(self, day: date):
        pool=await self.connect(); return await pool.fetchval("SELECT text FROM briefs WHERE user_id=$1 AND day=$2", current_user_id(), day)

    async def put_brief(self, day: date, text: str):
        pool=await self.connect(); await pool.execute("INSERT INTO briefs(user_id,day,text) VALUES($1,$2,$3) ON CONFLICT(user_id,day) DO UPDATE SET text=EXCLUDED.text,updated_at=now()",current_user_id(),day,text)

    async def fetch_chat_messages(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the authenticated user's newest chat messages in display order."""
        pool = await self.connect()
        rows = await pool.fetch(
            "SELECT id,role,content,tool_calls,created_at FROM ("
            "SELECT id,role,content,tool_calls,created_at FROM chat_messages "
            "WHERE user_id=$1 ORDER BY created_at DESC,id DESC LIMIT $2) recent "
            "ORDER BY created_at,id",
            current_user_id(), limit,
        )
        return [_dict(row) or {} for row in rows]

    async def fetch_chat_messages_since(
        self, day_start: datetime, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Return chat messages from the current coaching day in display order.

        Selects the NEWEST `limit` messages first, then reorders them
        chronologically — so after a long day the coach still sees the
        most recent context, not the day's oldest messages.
        """
        pool = await self.connect()
        rows = await pool.fetch(
            "SELECT * FROM ("
            "  SELECT id,role,content,tool_calls,created_at FROM chat_messages "
            "  WHERE user_id=$1 AND created_at >= $2 "
            "  ORDER BY created_at DESC, id DESC LIMIT $3"
            ") sub ORDER BY created_at ASC, id ASC",
            current_user_id(), day_start, limit,
        )
        return [_dict(row) or {} for row in rows]

    async def insert_chat_message(
        self, role: str, content: str, tool_calls: Any = None
    ) -> dict[str, Any]:
        """Persist one chat turn for the authenticated user."""
        pool = await self.connect()
        row = await pool.fetchrow(
            "INSERT INTO chat_messages(user_id,role,content,tool_calls) "
            "VALUES($1,$2,$3,$4::jsonb) RETURNING id,role,content,tool_calls,created_at",
            current_user_id(), role, content,
            json.dumps(tool_calls) if tool_calls is not None else None,
        )
        return _dict(row) or {}

    async def insert_user_chat_message(self, content: str, daily_cap: int) -> dict[str, Any]:
        """Persist one user chat turn iff the daily cap allows it.

        The count (current logging day, 4am rollover) and the insert run in one
        transaction behind a per-user advisory lock, so two concurrent turns at
        cap-1 serialize instead of both passing a stale count. Raises
        ChatQuotaExceeded — and persists nothing — once the cap is reached.
        """
        pool = await self.connect()
        user_id = current_user_id()
        start, end = effective_day_window()
        async with pool.acquire() as conn, conn.transaction():
            await conn.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended('chat_cap:' || $1, 0))",
                str(user_id),
            )
            used = await conn.fetchval(
                "SELECT count(*) FROM chat_messages WHERE user_id=$1 AND role='user' "
                "AND created_at >= $2 AND created_at < $3",
                user_id, start, end,
            )
            if int(used) >= daily_cap:
                raise ChatQuotaExceeded(f"daily chat cap of {daily_cap} reached")
            row = await conn.fetchrow(
                "INSERT INTO chat_messages(user_id,role,content,tool_calls) "
                "VALUES($1,'user',$2,NULL) RETURNING id,role,content,tool_calls,created_at",
                user_id, content,
            )
            return _dict(row) or {}

    async def insert_coach_usage(self, model: str, input_tokens: int, output_tokens: int) -> None:
        """Record one coach provider call's token spend for the authenticated user."""
        pool = await self.connect()
        await pool.execute(
            "INSERT INTO coach_usage(user_id,model,input_tokens,output_tokens) "
            "VALUES($1,$2,$3,$4)",
            current_user_id(), model, input_tokens, output_tokens,
        )

    async def has_macro_targets(self) -> bool:
        pool = await self.connect()
        return bool(await pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM macro_targets WHERE user_id=$1)",
            current_user_id(),
        ))

    async def fetch_integration(self, provider: str) -> dict[str, Any] | None:
        """Return an integration internally; API routes must never expose tokens."""
        pool = await self.connect()
        return _dict(await pool.fetchrow(
            "SELECT provider,access_token,refresh_token,scopes,expires_at "
            "FROM integrations WHERE user_id=$1 AND lower(provider)=lower($2)",
            current_user_id(), provider,
        ))


meal_row = meal
