"""Typed PostgreSQL store for the macro tracker."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping
from uuid import uuid4

import asyncpg


class StoreError(RuntimeError):
    pass


def _value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
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

    async def fetch_meals(self, start: date, end: date | None = None):
        pool = await self.connect(); end = end or start
        return [meal(r) for r in await pool.fetch("SELECT * FROM nutrition_entries WHERE day BETWEEN $1 AND $2 ORDER BY day, created_at DESC", start, end)]

    async def fetch_targets(self, day: date):
        pool = await self.connect(); r = await pool.fetchrow("SELECT * FROM macro_targets WHERE effective_date <= $1 ORDER BY effective_date DESC, created_at DESC LIMIT 1", day)
        return _dict(r)

    async def fetch_presets(self, active_only=True):
        pool = await self.connect(); where = "WHERE active" if active_only else ""
        return [_dict(r) for r in await pool.fetch(f"SELECT * FROM meal_presets {where} ORDER BY sort_order, lower(name)")]

    async def fetch_workouts(self, start: date | None = None, end: date | None = None, exercise: str | None = None):
        pool = await self.connect(); clauses=[]; args=[]
        if start is not None: args.append(start); clauses.append(f"day >= ${len(args)}")
        if end is not None: args.append(end); clauses.append(f"day <= ${len(args)}")
        if exercise is not None: args.append(exercise); clauses.append(f"exercise_name = ${len(args)}")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await pool.fetch("SELECT * FROM fitness_tracker" + where + " ORDER BY day DESC NULLS LAST, created_at DESC", *args)
        return [workout(r) for r in rows]

    async def fetch_prs(self):
        pool=await self.connect(); return [_dict(r) for r in await pool.fetch("SELECT * FROM exercise_max_reps ORDER BY date_achieved DESC NULLS LAST")]

    async def insert_meal(self, *, name, meal, calories, protein, carbs, fat, fiber, day, macro_source):
        pool=await self.connect(); id=str(uuid4()); r=await pool.fetchrow("INSERT INTO nutrition_entries(id,name,meal,calories,protein,carbs,fat,fiber,day,macro_source) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *",id,name,meal,calories,protein,carbs,fat,fiber,day,macro_source); return meal_row(r)

    async def insert_workout(self, *, exercise, workout_type, muscle_group, sets, day):
        pool=await self.connect(); id=str(uuid4()); vals=[]
        for i in range(4): vals += [sets[i]["weight"], sets[i]["reps"]] if i < len(sets) else [None,None]
        r=await pool.fetchrow("INSERT INTO fitness_tracker(id,exercise_name,workout_type,muscle_group,weight_1,reps_1,weight_2,reps_2,weight_3,reps_3,weight_4,reps_4,day) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING *",id,exercise,workout_type,muscle_group,*vals,day); return workout(r)

    async def save_preset(self, values: Mapping[str, Any], existing_id: str | None = None):
        pool=await self.connect(); id=existing_id or str(uuid4())
        r=await pool.fetchrow("INSERT INTO meal_presets(id,name,emoji,calories,protein,carbs,fat,fiber,meal,sort_order,active) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,true) ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,emoji=EXCLUDED.emoji,calories=EXCLUDED.calories,protein=EXCLUDED.protein,carbs=EXCLUDED.carbs,fat=EXCLUDED.fat,fiber=EXCLUDED.fiber,meal=EXCLUDED.meal,active=true RETURNING *",id,values['name'],values['emoji'],values['calories'],values['protein'],values['carbs'],values['fat'],values['fiber'],values['meal'],values.get('sort_order',0)); return _dict(r)

    async def insert_targets(self, day: date, values: Mapping[str, float]):
        pool=await self.connect(); id=str(uuid4()); r=await pool.fetchrow("INSERT INTO macro_targets(id,name,calories,protein,carbs,fat,fiber,effective_date) VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *",id,f"Targets from {day.isoformat()}",values['calories'],values['protein'],values['carbs'],values['fat'],values['fiber'],day); return _dict(r)

    async def delete(self, table: str, id: str):
        if table not in {'nutrition_entries','fitness_tracker'}: raise ValueError('invalid table')
        pool=await self.connect(); await pool.execute(f"DELETE FROM {table} WHERE id=$1", id)

    async def get_brief(self, day: date):
        pool=await self.connect(); return await pool.fetchval("SELECT text FROM briefs WHERE day=$1", day)

    async def put_brief(self, day: date, text: str):
        pool=await self.connect(); await pool.execute("INSERT INTO briefs(day,text) VALUES($1,$2) ON CONFLICT(day) DO UPDATE SET text=EXCLUDED.text,updated_at=now()",day,text)


meal_row = meal
