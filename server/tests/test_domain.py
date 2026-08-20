from datetime import date, datetime

import pytest

import domain
from domain import (
    LOCAL_TZ,
    MacroError,
    atwater_warning,
    average_totals,
    day_label,
    effective_date,
    normalize_meal,
    parse_date,
    remaining,
    resolve_date,
    sum_macros,
    validate_macro_source,
    validate_macros,
    validate_name,
)


def local(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=LOCAL_TZ)


# --------------------------------------------------------------------------- #
# The day boundary — the five cases from the spec
# --------------------------------------------------------------------------- #

# Saturday 2026-07-25 into Sunday 2026-07-26.
@pytest.mark.parametrize(
    "moment, expected",
    [
        (local(2026, 7, 25, 23, 59), date(2026, 7, 25)),  # Sat 11:59pm -> Saturday
        (local(2026, 7, 26, 0, 1), date(2026, 7, 25)),  # Sun 12:01am -> Saturday
        (local(2026, 7, 26, 3, 59), date(2026, 7, 25)),  # Sun 3:59am  -> Saturday
        (local(2026, 7, 26, 4, 1), date(2026, 7, 26)),  # Sun 4:01am  -> Sunday
        (local(2026, 7, 26, 14, 0), date(2026, 7, 26)),  # Sun 2:00pm  -> Sunday
    ],
)
def test_day_boundary_cases(moment, expected):
    assert effective_date(moment) == expected


def test_day_boundary_exactly_at_four_am_starts_the_new_day():
    assert effective_date(local(2026, 7, 26, 4, 0)) == date(2026, 7, 26)


def test_day_boundary_converts_other_timezones():
    from zoneinfo import ZoneInfo

    # 2026-07-26 03:30 UTC is 2026-07-25 23:30 Eastern -> still Saturday.
    utc_moment = datetime(2026, 7, 26, 3, 30, tzinfo=ZoneInfo("UTC"))
    assert effective_date(utc_moment) == date(2026, 7, 25)


def test_day_boundary_across_dst_fall_back():
    # 2026-11-01 is the US DST fall-back date; 1:30am local still belongs to Oct 31.
    assert effective_date(local(2026, 11, 1, 1, 30)) == date(2026, 10, 31)


def test_effective_date_defaults_to_now_and_is_a_date():
    assert isinstance(effective_date(), date)


def test_day_label():
    assert day_label(date(2026, 7, 25)) == "Saturday, July 25"
    assert day_label(date(2026, 1, 5)) == "Monday, January 5"


def test_workout_streak_counts_only_completed_monday_to_sunday_weeks():
    rows = [
        {"date": "2026-07-27"},
        {"date": "2026-07-29"},
        {"date": "2026-08-02"},
        {"date": "2026-08-03"},
        {"date": "2026-08-05"},
        {"date": "2026-08-09"},
        # The in-progress week qualifies so far, but must not count yet.
        {"date": "2026-08-10"},
        {"date": "2026-08-12"},
        {"date": "2026-08-14"},
    ]

    assert domain.workout_week_stats(rows, date(2026, 8, 10))["week"]["streak_weeks"] == 2
    assert domain.workout_week_stats(rows, date(2026, 8, 16))["week"]["streak_weeks"] == 2


def test_workout_streak_includes_the_week_after_sunday_has_elapsed():
    rows = [
        {"date": "2026-08-03"},
        {"date": "2026-08-05"},
        {"date": "2026-08-09"},
        {"date": "2026-08-10"},
        {"date": "2026-08-12"},
        {"date": "2026-08-16"},
    ]

    assert domain.workout_week_stats(rows, date(2026, 8, 16))["week"]["streak_weeks"] == 1
    assert domain.workout_week_stats(rows, date(2026, 8, 17))["week"]["streak_weeks"] == 2


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #


def test_parse_date_ok():
    assert parse_date("2026-07-25") == date(2026, 7, 25)


@pytest.mark.parametrize("bad", ["07/25/2026", "2026-13-01", "", "  ", "yesterday"])
def test_parse_date_rejects_bad_input(bad):
    with pytest.raises(MacroError):
        parse_date(bad)


def test_resolve_date_defaults_to_effective_date():
    assert resolve_date(None) == effective_date()
    assert resolve_date("  ") == effective_date()
    assert resolve_date("2026-02-03") == date(2026, 2, 3)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("breakfast", "Breakfast"),
        ("LUNCH", "Lunch"),
        ("  dinner ", "Dinner"),
        ("Snack", "Snack"),
        (None, "Snack"),
        ("", "Snack"),
    ],
)
def test_normalize_meal(raw, expected):
    assert normalize_meal(raw) == expected


def test_normalize_meal_rejects_unknown_with_a_useful_message():
    with pytest.raises(MacroError) as excinfo:
        normalize_meal("brunch")
    message = str(excinfo.value)
    assert "Breakfast, Lunch, Dinner, Snack" in message
    assert "brunch" in message


def test_normalize_meal_custom_default():
    assert normalize_meal(None, default="Dinner") == "Dinner"


def test_validate_macros_rounds_and_coerces():
    assert validate_macros(150, 30, "3", 2.505) == {
        "calories": 150.0,
        "protein": 30.0,
        "carbs": 3.0,
        "fat": 2.5,
        "fiber": 0.0,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"calories": -1, "protein": 0, "carbs": 0, "fat": 0},
        {"calories": float("nan"), "protein": 0, "carbs": 0, "fat": 0},
        {"calories": float("inf"), "protein": 0, "carbs": 0, "fat": 0},
        {"calories": 10001, "protein": 0, "carbs": 0, "fat": 0},
        {"calories": 100, "protein": 1001, "carbs": 0, "fat": 0},
        {"calories": 100, "protein": 0, "carbs": 0, "fat": "lots"},
    ],
)
def test_validate_macros_rejects_bad_numbers(kwargs):
    with pytest.raises(MacroError):
        validate_macros(**kwargs)


def test_validate_name():
    assert validate_name("  Chicken breast ") == "Chicken breast"
    with pytest.raises(MacroError):
        validate_name("   ")
    with pytest.raises(MacroError):
        validate_name("x" * 201)


@pytest.mark.parametrize(
    "source",
    [
        "FDA FoodData Central: chicken breast, roasted",
        "Fairlife Core Power label",
        "user provided from package",
    ],
)
def test_validate_macro_source_accepts_real_sources(source):
    assert validate_macro_source(source) == source


@pytest.mark.parametrize(
    "source",
    ["", "   ", "estimate", "APPROX", " guess ", "unknown", "n/a", "best guess", "?"],
)
def test_validate_macro_source_rejects_placeholders(source):
    with pytest.raises(MacroError) as excinfo:
        validate_macro_source(source)
    assert "FDA FoodData Central" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Atwater cross-check
# --------------------------------------------------------------------------- #


def test_atwater_consistent_entry_has_no_warning():
    # 30p + 3c + 2.5f -> 154.5 kcal against 150 logged.
    assert atwater_warning(150, 30, 3, 2.5) is None


def test_atwater_small_absolute_difference_is_tolerated():
    # 4/4/9 says 160 kcal against 200 logged: 40 off, inside the 50 kcal floor.
    assert atwater_warning(200, 30, 10, 0) is None


def test_atwater_flags_a_transcription_error():
    warning = atwater_warning(1500, 30, 3, 2.5)
    assert warning is not None
    assert "1500" in warning and "Atwater" in warning


def test_atwater_uses_twenty_percent_band_on_large_entries():
    # 1000 kcal logged, 4/4/9 says 800 -> 200 off, tolerance 200 -> no warning.
    assert atwater_warning(1000, 100, 100, 0) is None
    # Push it past 20%.
    assert atwater_warning(1000, 90, 90, 0) is not None


def test_atwater_tolerates_zero_calorie_entries():
    assert atwater_warning(0, 0, 0, 0) is None


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #

MEALS = [
    {"calories": 150, "protein": 30, "carbs": 3, "fat": 2.5},
    {"calories": 620, "protein": 68, "carbs": 45, "fat": 18},
    {"calories": None, "protein": None, "carbs": None, "fat": None},
    {},
]


def test_sum_macros_handles_null_and_missing_numbers():
    assert sum_macros(MEALS) == {
        "calories": 770.0,
        "protein": 98.0,
        "carbs": 48.0,
        "fat": 20.5,
        "fiber": 0.0,
    }


def test_sum_macros_of_nothing_is_zero():
    assert sum_macros([]) == {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0, "fiber": 0.0}


def test_remaining_can_go_negative():
    totals = {"calories": 2600, "protein": 120, "carbs": 200, "fat": 70}
    targets = {"calories": 2400, "protein": 215, "carbs": 200, "fat": 70}
    assert remaining(totals, targets) == {
        "calories": -200.0,
        "protein": 95.0,
        "carbs": 0.0,
        "fat": 0.0,
        "fiber": 0.0,
    }


def test_remaining_treats_missing_targets_as_zero():
    assert remaining({"calories": 100}, {}) == {
        "calories": -100.0,
        "protein": 0.0,
        "carbs": 0.0,
        "fat": 0.0,
        "fiber": 0.0,
    }


def test_average_totals():
    days = [
        {"calories": 2000, "protein": 200, "carbs": 150, "fat": 60},
        {"calories": 2400, "protein": 220, "carbs": 170, "fat": 70},
    ]
    assert average_totals(days) == {
        "calories": 2200.0,
        "protein": 210.0,
        "carbs": 160.0,
        "fat": 65.0,
        "fiber": 0.0,
    }
    assert average_totals([]) == domain.zero_totals()


# --------------------------------------------------------------------------- #
# Workout validation
# --------------------------------------------------------------------------- #

from domain import (  # noqa: E402
    next_workout_type,
    normalize_muscle_group,
    normalize_workout_type,
    validate_sets,
    workout_type_from_muscle,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("push", "Push"),
        ("PULL", "Pull"),
        ("  legs ", "Legs"),
        ("Abs", "Abs"),
        ("cardio", "Cardio"),
        ("full body", "Full Body"),
    ],
)
def test_normalize_workout_type(raw, expected):
    assert normalize_workout_type(raw) == expected


def test_normalize_workout_type_rejects_unknown():
    with pytest.raises(MacroError) as excinfo:
        normalize_workout_type("yoga")
    assert "Push, Pull, Legs, Abs, Cardio, Full Body" in str(excinfo.value)


def test_normalize_workout_type_requires_a_value():
    with pytest.raises(MacroError):
        normalize_workout_type(None)
    with pytest.raises(MacroError):
        normalize_workout_type("")


def test_normalize_muscle_group_maps_workout_types():
    assert normalize_muscle_group(["Push"]) == ["Push"]
    assert normalize_muscle_group(["Pull"]) == ["Pull"]
    assert normalize_muscle_group(["Legs"]) == ["Quads"]
    assert normalize_muscle_group(["Abs"]) == ["Abs"]


def test_validate_sets_ok():
    sets = [{"weight": 225, "reps": 5}, {"weight": 185, "reps": 8}]
    assert validate_sets(sets) == [
        {"weight": 225.0, "reps": 5.0},
        {"weight": 185.0, "reps": 8.0},
    ]


def test_validate_sets_allows_bodyweight_zero_weight():
    assert validate_sets([{"weight": 0, "reps": 10}]) == [
        {"weight": 0.0, "reps": 10.0}
    ]


@pytest.mark.parametrize(
    "sets",
    [
        [],
        "not a list",
        [{"weight": -5, "reps": 10}],
        [{"weight": 225, "reps": -1}],
        [{"weight": 99999, "reps": 10}],
        [{"weight": 225, "reps": 9999}],
        [{"weight": 0, "reps": 0}],
        [{"weight": "heavy", "reps": 10}],
        [{"weight": 225}],
        [{"weight": 225, "reps": 10}, {"weight": 225, "reps": 10}, {"weight": 225, "reps": 10}, {"weight": 225, "reps": 10}, {"weight": 225, "reps": 10}],
    ],
)
def test_validate_sets_rejects_bad_input(sets):
    with pytest.raises(MacroError):
        validate_sets(sets)


# --------------------------------------------------------------------------- #
# Workout rotation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "last, expected",
    [
        ("Push", "Pull"),
        ("Pull", "Legs"),
        ("Legs", "Push"),
        ("push", "Pull"),
        ("  PULL ", "Legs"),
        (None, "Push"),
        ("Abs", "Push"),  # accessory days do not advance the split
        ("Cardio", "Push"),
        ("yoga", "Push"),
    ],
)
def test_next_workout_type_rotates(last, expected):
    assert next_workout_type(last) == expected


def test_next_workout_type_cycles_forever():
    day = "Legs"
    seen = []
    for _ in range(6):
        day = next_workout_type(day)
        seen.append(day)
    assert seen == ["Push", "Pull", "Legs", "Push", "Pull", "Legs"]


def test_workout_type_from_muscle():
    assert workout_type_from_muscle(["Quads", "Abs"]) == "Legs"
    assert workout_type_from_muscle(["Hams"]) == "Legs"
    assert workout_type_from_muscle(["Push"]) == "Push"
    assert workout_type_from_muscle([]) is None
    assert workout_type_from_muscle(["SomethingElse"]) is None
