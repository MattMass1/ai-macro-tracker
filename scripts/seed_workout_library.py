#!/usr/bin/env python3
"""Idempotently seed the shared workout exercise library."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/src"))

import domain  # noqa: E402

SCHEMA = ROOT / "server/src/schema.sql"

# name, muscle groups, workout type, equipment, difficulty, swaps
CURATED = [
    ("Bench Press", ["Chest", "Triceps"], "Push", "barbell", "intermediate", ["Incline DB Press", "Machine Chest Press"]),
    ("Incline DB Press", ["Chest", "Triceps"], "Push", "dumbbell", "intermediate", ["Bench Press", "Machine Chest Press"]),
    ("Machine Chest Press", ["Chest", "Triceps"], "Push", "machine", "beginner", ["Bench Press", "Incline DB Press"]),
    ("Overhead Press", ["Shoulders", "Triceps"], "Push", "barbell", "intermediate", ["Dumbbell Shoulder Press", "Machine Shoulder Press"]),
    ("Dumbbell Shoulder Press", ["Shoulders", "Triceps"], "Push", "dumbbell", "beginner", ["Overhead Press", "Machine Shoulder Press"]),
    ("Machine Shoulder Press", ["Shoulders", "Triceps"], "Push", "machine", "beginner", ["Overhead Press", "Dumbbell Shoulder Press"]),
    ("Lateral Raises", ["Shoulders"], "Push", "dumbbell", "beginner", ["Cable Lateral Raises"]),
    ("Cable Lateral Raises", ["Shoulders"], "Push", "cable", "beginner", ["Lateral Raises"]),
    ("Triceps Pushdown", ["Triceps"], "Push", "cable", "beginner", ["Overhead Triceps Extension", "Skull Crushers"]),
    ("Overhead Triceps Extension", ["Triceps"], "Push", "cable", "beginner", ["Triceps Pushdown", "Skull Crushers"]),
    ("Skull Crushers", ["Triceps"], "Push", "barbell", "intermediate", ["Triceps Pushdown", "Overhead Triceps Extension"]),
    ("Barbell Row", ["Back", "Biceps"], "Pull", "barbell", "intermediate", ["Seated Cable Row", "Chest-Supported DB Row"]),
    ("Seated Cable Row", ["Back", "Biceps"], "Pull", "cable", "beginner", ["Barbell Row", "Chest-Supported DB Row"]),
    ("Chest-Supported DB Row", ["Back", "Biceps"], "Pull", "dumbbell", "beginner", ["Barbell Row", "Seated Cable Row"]),
    ("Lat Pulldown", ["Lats", "Biceps"], "Pull", "cable", "beginner", ["Pull-ups", "Assisted Pull-ups"]),
    ("Pull-ups", ["Lats", "Biceps"], "Pull", "bodyweight", "advanced", ["Lat Pulldown", "Assisted Pull-ups"]),
    ("Assisted Pull-ups", ["Lats", "Biceps"], "Pull", "machine", "beginner", ["Lat Pulldown", "Pull-ups"]),
    ("Face Pulls", ["Rear Delts", "Upper Back"], "Pull", "cable", "beginner", ["Reverse Pec Deck"]),
    ("Reverse Pec Deck", ["Rear Delts", "Upper Back"], "Pull", "machine", "beginner", ["Face Pulls"]),
    ("Barbell Curl", ["Biceps"], "Pull", "barbell", "beginner", ["Dumbbell Curl", "Hammer Curl"]),
    ("Dumbbell Curl", ["Biceps"], "Pull", "dumbbell", "beginner", ["Barbell Curl", "Hammer Curl"]),
    ("Hammer Curl", ["Biceps", "Forearms"], "Pull", "dumbbell", "beginner", ["Dumbbell Curl", "Cable Curl"]),
    ("Cable Curl", ["Biceps"], "Pull", "cable", "beginner", ["Barbell Curl", "Dumbbell Curl"]),
    ("Barbell Squat", ["Quads", "Glutes"], "Legs", "barbell", "intermediate", ["Leg Press", "Hack Squats"]),
    ("Hack Squats", ["Quads", "Glutes"], "Legs", "machine", "intermediate", ["Barbell Squat", "Leg Press"]),
    ("Leg Press", ["Quads", "Glutes"], "Legs", "machine", "beginner", ["Barbell Squat", "Hack Squats"]),
    ("Romanian Deadlift", ["Hams", "Glutes"], "Legs", "barbell", "intermediate", ["Dumbbell Romanian Deadlift", "Leg Curls"]),
    ("Dumbbell Romanian Deadlift", ["Hams", "Glutes"], "Legs", "dumbbell", "beginner", ["Romanian Deadlift", "Leg Curls"]),
    ("Leg Curls", ["Hams"], "Legs", "machine", "beginner", ["Romanian Deadlift", "Dumbbell Romanian Deadlift"]),
    ("Bulgarian Split Squat", ["Quads", "Glutes"], "Legs", "dumbbell", "intermediate", ["Walking Lunges", "Leg Press"]),
    ("Walking Lunges", ["Quads", "Glutes"], "Legs", "dumbbell", "intermediate", ["Bulgarian Split Squat", "Leg Press"]),
    ("Leg Extension", ["Quads"], "Legs", "machine", "beginner", ["Leg Press", "Hack Squats"]),
    ("Standing Calf Raise", ["Calves"], "Legs", "machine", "beginner", ["Seated Calf Raise"]),
    ("Seated Calf Raise", ["Calves"], "Legs", "machine", "beginner", ["Standing Calf Raise"]),
    ("Deadlift", ["Back", "Hams", "Glutes"], "Full Body", "barbell", "advanced", ["Romanian Deadlift"]),
    ("Kettlebell Swing", ["Glutes", "Hams", "Core"], "Full Body", "kettlebell", "intermediate", ["Deadlift"]),
]

DEFAULT_METADATA = {
    "High-to-Low Cable Fly (cable/rope)": (["Chest"], "cable", "intermediate", ["Cable Decline Press (cable/rope)", "Decline DB Bench Press"]),
    "Cable Decline Press (cable/rope)": (["Chest", "Triceps"], "cable", "intermediate", ["Decline DB Bench Press", "High-to-Low Cable Fly (cable/rope)"]),
    "Dips (forward lean, bodyweight)": (["Chest", "Triceps"], "bodyweight", "advanced", ["Decline DB Bench Press", "Bench Press"]),
    "Decline DB Bench Press": (["Chest", "Triceps"], "dumbbell", "intermediate", ["Dips (forward lean, bodyweight)", "Bench Press"]),
    "Back Extension": (["Hams", "Glutes", "Lower Back"], "bodyweight", "beginner", ["Romanian Deadlift"]),
    "Smith Machine Squats": (["Quads", "Glutes"], "machine", "beginner", ["Hack Squats", "Leg Press"]),
    "Crunches": (["Abs"], "bodyweight", "beginner", ["Cable Crunches", "Ab Wheel"]),
    "Hanging Leg Raises": (["Abs"], "bodyweight", "advanced", ["Crunches", "Ab Wheel"]),
    "Planks": (["Abs"], "bodyweight", "beginner", ["Ab Wheel", "Crunches"]),
    "Cable Crunches": (["Abs"], "cable", "beginner", ["Crunches", "Ab Wheel"]),
    "Russian Twists": (["Abs"], "bodyweight", "beginner", ["Planks", "Cable Crunches"]),
    "Ab Wheel": (["Abs"], "bodyweight", "advanced", ["Planks", "Cable Crunches"]),
    "Treadmill": (["Cardio"], "machine", "beginner", ["Bike", "Stairmaster"]),
    "Bike": (["Cardio"], "machine", "beginner", ["Treadmill", "Rowing Machine"]),
    "Stairmaster": (["Cardio"], "machine", "intermediate", ["Treadmill", "Bike"]),
    "Rowing Machine": (["Cardio"], "machine", "intermediate", ["Bike", "Treadmill"]),
}


def seed_rows() -> list[tuple[object, ...]]:
    """Combine curated lifts with every legacy domain default by name."""
    rows = {row[0]: row for row in CURATED}
    for workout_type in domain.WORKOUT_TYPES:
        for name in domain.get_default_exercises_for_type(workout_type):
            if name in rows:
                continue
            muscles, equipment, difficulty, swaps = DEFAULT_METADATA[name]
            rows[name] = (name, muscles, workout_type, equipment, difficulty, swaps)
    missing_swaps = sorted({swap for row in rows.values() for swap in row[5]} - rows.keys())
    if missing_swaps:
        raise RuntimeError(f"Swap targets missing from seed: {missing_swaps}")
    return list(rows.values())


async def run() -> None:
    """Apply additive schema and insert any missing library exercises."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    rows = seed_rows()
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(SCHEMA.read_text(encoding="utf-8"))
        before = await conn.fetchval("SELECT count(*) FROM workout_library")
        await conn.executemany(
            "INSERT INTO workout_library(name,muscle_group,workout_type,equipment,difficulty,swaps) "
            "VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(name) DO UPDATE SET "
            "muscle_group=EXCLUDED.muscle_group, workout_type=EXCLUDED.workout_type, "
            "equipment=EXCLUDED.equipment, difficulty=EXCLUDED.difficulty, swaps=EXCLUDED.swaps",
            rows,
        )
        after = await conn.fetchval("SELECT count(*) FROM workout_library")
    finally:
        await conn.close()
    print(f"Workout library: seed_rows={len(rows)} inserted={after - before} total={after}")


if __name__ == "__main__":
    asyncio.run(run())
