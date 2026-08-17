#!/usr/bin/env python3
"""Load the Notion JSON export into PostgreSQL. Safe to rerun (upserts by id)."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import asyncpg

EXPORT_DIR = Path("/opt/data/migration-export")
SCHEMA = Path(__file__).resolve().parents[1] / "server/src/schema.sql"


def prop(row: dict[str, Any], name: str) -> dict[str, Any]:
    return row.get("properties", {}).get(name) or {}


def text(row, name, kind="title"):
    return "".join(x.get("plain_text", "") for x in prop(row, name).get(kind, [])).strip()


def number(row, name): return prop(row, name).get("number")
def num(row, name): return number(row, name) or 0
def selected(row, name): return (prop(row, name).get("select") or {}).get("name")
def multi(row, name): return [x["name"] for x in prop(row, name).get("multi_select", []) if x.get("name")]
def day(row, name):
    value = (prop(row, name).get("date") or {}).get("start")
    if value:
        return date.fromisoformat(value[:10])
    # Fallback: original Notion code used created_time[:10] when the
    # user-entered date was empty. Match that so day-based queries sort right.
    return datetime.fromisoformat(row["created_time"].replace("Z", "+00:00")).date()
def created(row): return datetime.fromisoformat(row["created_time"].replace("Z", "+00:00"))
def relation(row, name):
    values = prop(row, name).get("relation", [])
    return values[0].get("id") if values else None


def rows(name):
    return [r for r in json.loads((EXPORT_DIR / f"{name}.json").read_text())
            if not (r.get("archived") or r.get("is_archived") or r.get("in_trash"))]


async def run() -> None:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url: raise SystemExit("DATABASE_URL is required")
    conn = await asyncpg.connect(url)
    counts = {}
    try:
        await conn.execute(SCHEMA.read_text())
        async with conn.transaction():
            data = rows("nutrition_entries")
            await conn.executemany("INSERT INTO nutrition_entries(id,name,meal,calories,protein,carbs,fat,fiber,day,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT(id) DO NOTHING", [(r['id'],text(r,'Name'),selected(r,'Meal') or 'Snack',num(r,'Calories'),num(r,'Protein (g)'),num(r,'Carbs (g)'),num(r,'Fat (g)'),num(r,'Fiber'),day(r,'Date'),created(r)) for r in data]); counts['nutrition_entries']=len(data)
            fitness = rows("fitness_tracker")
            await conn.executemany("INSERT INTO fitness_tracker(id,exercise_name,workout_type,muscle_group,weight_1,reps_1,weight_2,reps_2,weight_3,reps_3,weight_4,reps_4,day,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14) ON CONFLICT(id) DO NOTHING", [(r['id'],text(r,'Exercise Name'),multi(r,'Workout type'),multi(r,'Muscle Group'),*[number(r,f'{kind} {i}') for i in range(1,5) for kind in ('Weight','Reps')],day(r,'Date (user input)'),created(r)) for r in fitness]); counts['fitness_tracker']=len(fitness)
            maxes={r['id']:max([number(r,f'Weight {i}') or 0 for i in range(1,5)]) for r in fitness}
            data=rows('max_weight'); await conn.executemany("INSERT INTO max_weight(id,exercise_name,muscle_group,all_time_max,created_at) VALUES($1,$2,$3,$4,$5) ON CONFLICT(id) DO NOTHING",[(r['id'],text(r,'Exercise Name'),multi(r,'Muscle Group'),max([maxes.get(x.get('id'),0) for x in prop(r,'Primary Fitness Tracker').get('relation',[])] or [0]),created(r)) for r in data]); counts['max_weight']=len(data)
            data=rows('exercise_max_reps'); await conn.executemany("INSERT INTO exercise_max_reps(id,exercise,max_weight,date_achieved,workout_type,source_entry,created_at) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(id) DO NOTHING",[(r['id'],text(r,'Exercise'),num(r,'Max weight'),day(r,'Date achieved'),multi(r,'Workout type'),relation(r,'Source entry'),created(r)) for r in data]); counts['exercise_max_reps']=len(data)
            data=rows('meal_presets'); await conn.executemany("INSERT INTO meal_presets(id,name,emoji,calories,protein,carbs,fat,fiber,meal,sort_order,active,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) ON CONFLICT(id) DO NOTHING",[(r['id'],text(r,'Name'),text(r,'Emoji','rich_text') or '🍽️',num(r,'Calories'),num(r,'Protein'),num(r,'Carbs'),num(r,'Fat'),num(r,'Fiber'),selected(r,'Meal') or 'Dinner',num(r,'Sort Order'),bool(prop(r,'Active').get('checkbox')),created(r)) for r in data]); counts['meal_presets']=len(data)
            data=rows('macro_targets'); await conn.executemany("INSERT INTO macro_targets(id,name,calories,protein,carbs,fat,fiber,effective_date,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT(id) DO NOTHING",[(r['id'],text(r,'Name'),num(r,'Calories'),num(r,'Protein'),num(r,'Carbs'),num(r,'Fat'),num(r,'Fiber'),day(r,'Effective Date'),created(r)) for r in data]); counts['macro_targets']=len(data)
        for table, count in counts.items(): print(f"{table}: {count} row(s)")
        print(f"total: {sum(counts.values())} row(s)")
    finally: await conn.close()


if __name__ == "__main__": asyncio.run(run())
