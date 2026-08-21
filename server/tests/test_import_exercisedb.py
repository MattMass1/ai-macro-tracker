import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import import_exercisedb as importer  # noqa: E402
from scripts.seed_workout_library import seed_rows  # noqa: E402


def exercise(name="dumbbell bench press", body_parts=None, targets=None):
    return {
        "name": name,
        "bodyParts": body_parts or ["chest"],
        "targetMuscles": targets or ["pectorals"],
        "equipments": ["dumbbell"],
        "gifUrl": "https://static.exercisedb.dev/media/bench.gif",
        "instructions": ["Lower with control.", "Press upward."],
    }


def test_body_parts_map_to_canonical_workout_types():
    cases = [
        (["chest"], ["pectorals"], "Push"),
        (["back"], ["lats"], "Pull"),
        (["upper legs"], ["quadriceps"], "Legs"),
        (["waist"], ["abdominals"], "Abs"),
        (["cardio"], [], "Cardio"),
        (["neck"], [], "Full Body"),
    ]
    for body_parts, targets, expected in cases:
        _, workout_type = importer.map_muscles_and_type(
            exercise(body_parts=body_parts, targets=targets)
        )
        assert workout_type == expected


def test_import_dedupe_preserves_curated_names_and_source_duplicates():
    curated = seed_rows()
    source = [
        exercise(name="Bench Press"), exercise(),
        exercise(name="DUMBBELL BENCH PRESS"), exercise(name="Cable Fly"),
    ]
    rows = importer.new_rows(source, [row[0] for row in curated])

    assert [row[0] for row in rows] == ["Dumbbell Bench Press", "Cable Fly"]
    assert len(curated) + len(rows) == 54
    assert all(row[5] == [] for row in rows)


def test_import_row_has_media_and_instructions():
    row = importer.exercise_to_row(exercise())

    assert row[0] == "Dumbbell Bench Press"
    assert row[2] == "Push"
    assert row[6].endswith("bench.gif")
    assert row[7] == "Lower with control.\nPress upward."
