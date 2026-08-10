"use client";

import { useMemo, useRef, useState } from "react";

import type {
  KnownExercise,
  LastWorkoutPayload,
  WorkoutSet,
} from "@/lib/types";
import { WORKOUT_TYPES } from "@/lib/types";

type Props = {
  exercises: KnownExercise[];
  todayPlan?: { type: string; exercises: { name: string }[] };
  pending: boolean;
  error: string | null;
  onLog: (
    exercise: string,
    sets: WorkoutSet[],
    workoutType: string,
  ) => Promise<boolean>;
};

const EMPTY_SET: WorkoutSet = { weight: 0, reps: 0 };

/** Default abs / cardio exercises always available as quick-pick chips. */
const ABS_DEFAULTS = [
  "Crunches",
  "Hanging Leg Raises",
  "Planks",
  "Cable Crunches",
  "Russian Twists",
  "Ab Wheel",
];

const CARDIO_DEFAULTS = [
  "Treadmill",
  "Bike",
  "Stairmaster",
  "Rowing Machine",
];

function typeEmoji(workoutType: string): string {
  switch (workoutType) {
    case "Push":
      return "🔥";
    case "Pull":
      return "🏋️";
    case "Legs":
      return "🦵";
    case "Abs":
      return "💪";
    case "Cardio":
      return "🏃";
    case "Rest":
      return "🛌";
    default:
      return "💪";
  }
}

/**
 * Log one exercise with up to 4 sets. Exercise comes from a picker built
 * from the PR log (known exercises), or free text for anything new.
 */
export default function WorkoutLogger({
  exercises,
  todayPlan,
  pending,
  error,
  onLog,
}: Props) {
  const [exercise, setExercise] = useState("");
  const [workoutType, setWorkoutType] = useState<string>("Push");
  const [sets, setSets] = useState<WorkoutSet[]>([{ ...EMPTY_SET }]);
  const [lastWorkout, setLastWorkout] = useState<LastWorkoutPayload | null>(
    null,
  );
  const requestId = useRef(0);

  const known = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const ex of exercises) map.set(ex.name, ex.workout_type);
    return map;
  }, [exercises]);

  const grouped = useMemo(() => {
    const groups = new Map<string, string[]>();
    for (const ex of exercises) {
      const type = ex.workout_type[0] ?? "Other";
      const list = groups.get(type) ?? [];
      list.push(ex.name);
      groups.set(type, list);
    }
    return [...groups.entries()];
  }, [exercises]);

  const chipExercises = useMemo(() => {
    const fromGroup = grouped.find(([type]) => type === workoutType);
    return fromGroup ? fromGroup[1] : todayPlan?.exercises.map((e) => e.name) ?? [];
  }, [grouped, workoutType, todayPlan]);

  /** Abs / cardio chips: defaults merged with known exercises so history
   *  exercises also appear alongside the static defaults. */
  const absChips = useMemo(() => {
    const fromKnown = exercises
      .filter((ex) => ex.workout_type.includes("Abs"))
      .map((ex) => ex.name);
    return [...new Set([...ABS_DEFAULTS, ...fromKnown])];
  }, [exercises]);

  const cardioChips = useMemo(() => {
    const fromKnown = exercises
      .filter((ex) => ex.workout_type.includes("Cardio"))
      .map((ex) => ex.name);
    return [...new Set([...CARDIO_DEFAULTS, ...fromKnown])];
  }, [exercises]);

  const loadLastWorkout = async (name: string) => {
    const currentRequest = ++requestId.current;
    setLastWorkout(null);
    try {
      const response = await fetch(
        `/api/macro/workouts/last?exercise=${encodeURIComponent(name)}`,
        { cache: "no-store" },
      );
      if (!response.ok) return;
      const payload = (await response.json()) as LastWorkoutPayload;
      if (currentRequest !== requestId.current) return;
      const capped = payload.sets.slice(0, 4);
      setLastWorkout(capped.length > 0 ? { ...payload, sets: capped } : null);
    } catch {
      // Last-session lookup is optional and should never block logging.
    }
  };

  const pickExercise = (name: string, plannedType?: string) => {
    setExercise(name);
    setSets([{ ...EMPTY_SET }]);
    if (plannedType) setWorkoutType(plannedType);
    else {
      const types = known.get(name);
      if (types && types.length > 0) setWorkoutType(types[0]);
    }
    void loadLastWorkout(name);
  };

  const updateSet = (index: number, field: keyof WorkoutSet, value: string) => {
    setSets((prev) =>
      prev.map((set, i) =>
        i === index ? { ...set, [field]: Number(value) || 0 } : set,
      ),
    );
  };

  const addSet = () => {
    setSets((prev) => (prev.length < 4 ? [...prev, { ...EMPTY_SET }] : prev));
  };

  const removeSet = (index: number) => {
    setSets((prev) =>
      prev.length > 1 ? prev.filter((_, i) => i !== index) : prev,
    );
  };

  const hasValues = sets.some((set) => set.weight > 0 || set.reps > 0);
  const isRest = workoutType === "Rest";

  const submit = async () => {
    if (!isRest && (!exercise.trim() || !hasValues)) return;
    const logged = await onLog(
      isRest ? "Rest Day" : exercise.trim(),
      isRest
        ? []
        : sets.filter((set) => set.weight > 0 || set.reps > 0),
      workoutType,
    );
    if (logged) {
      requestId.current += 1;
      setExercise("");
      setSets([{ ...EMPTY_SET }]);
      setLastWorkout(null);
    }
  };

  return (
    <section className="rounded-2xl bg-surface p-4">
      <h2 className="mb-3 text-xs font-semibold uppercase tracking-[0.18em] text-muted">
        Log an exercise
      </h2>

      {chipExercises.length > 0 && (
        <div className="mb-3">
          <p className="mb-1.5 text-xs font-semibold text-muted">
            Quick pick: {workoutType.toUpperCase()}
          </p>
          <div className="flex flex-wrap gap-1.5">
            {chipExercises.map((name) => (
              <button
                key={name}
                type="button"
                onClick={() => pickExercise(name, workoutType)}
                className="rounded-full bg-surface-2 px-2.5 py-1.5 text-xs active:opacity-70"
              >
                {name}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Abs / Cardio add-on chips: appear as add-ons for strength days,
          standalone for Rest, and complementary for Abs/Cardio types. */}
      {(absChips.length > 0 || cardioChips.length > 0) && (
        <div className="mb-3">
          <p className="mb-1.5 text-xs font-semibold text-muted">
            {workoutType === "Rest"
              ? "Quick add"
              : workoutType === "Abs"
                ? "Add-on · Cardio"
                : workoutType === "Cardio"
                  ? "Add-on · Abs"
                  : "Add-ons"}
          </p>
          <div className="flex flex-wrap gap-1.5">
            {workoutType !== "Abs" &&
              absChips.map((name) => (
                <button
                  key={`abs-${name}`}
                  type="button"
                  onClick={() => pickExercise(name, "Abs")}
                  className="rounded-full bg-surface-2 px-2.5 py-1.5 text-xs active:opacity-70"
                >
                  💪 {name}
                </button>
              ))}
            {workoutType !== "Cardio" &&
              cardioChips.map((name) => (
                <button
                  key={`cardio-${name}`}
                  type="button"
                  onClick={() => pickExercise(name, "Cardio")}
                  className="rounded-full bg-surface-2 px-2.5 py-1.5 text-xs active:opacity-70"
                >
                  🏃 {name}
                </button>
              ))}
          </div>
        </div>
      )}

      <div className="mb-1 flex items-center justify-between">
        <label className="block text-xs text-muted" htmlFor="exercise">
          Exercise
        </label>
        {lastWorkout && (
          <button
            type="button"
            onClick={() => setSets(lastWorkout.sets.map((set) => ({ ...set })))}
            className="text-xs font-semibold text-protein"
          >
            Fill last time
          </button>
        )}
      </div>
      <input
        id="exercise"
        list="known-exercises"
        value={exercise}
        onChange={(e) => {
          setExercise(e.target.value);
          const types = known.get(e.target.value);
          if (types && types.length > 0) {
            setWorkoutType(types[0]);
            void loadLastWorkout(e.target.value);
          } else {
            requestId.current += 1;
            setLastWorkout(null);
          }
        }}
        placeholder="Barbell Bench Press"
        className="mb-3 w-full rounded-xl bg-surface-2 px-3 py-2.5 text-base outline-none placeholder:text-muted/50 focus:ring-2 focus:ring-protein/40"
      />
      <datalist id="known-exercises">
        {grouped.map(([type, names]) => (
          <optgroup key={type} label={`${typeEmoji(type)} ${type}`}>
            {names.map((name) => (
              <option key={name} value={name} />
            ))}
          </optgroup>
        ))}
      </datalist>

      <div className="mb-3 flex flex-wrap gap-2">
        {[...WORKOUT_TYPES, "Rest"].map((type) => (
          <button
            key={type}
            type="button"
            onClick={() => {
              if (type === workoutType) return;
              requestId.current += 1;
              setWorkoutType(type);
              setExercise("");
              setSets([{ ...EMPTY_SET }]);
              setLastWorkout(null);
            }}
            className={`min-h-9 rounded-full px-3 text-xs font-semibold transition-colors ${
              workoutType === type
                ? "bg-protein text-black"
                : "bg-surface-2 text-muted active:bg-surface-2/60"
            }`}
          >
            {typeEmoji(type)} {type}
          </button>
        ))}
      </div>

      {!isRest && (
        <>
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs text-muted">Sets</span>
            {sets.length < 4 && (
              <button
                type="button"
                onClick={addSet}
                className="text-xs font-semibold text-protein"
              >
                + Add set
              </button>
            )}
          </div>

          <div className="space-y-2">
            {sets.map((set, index) => (
              <div key={index} className="flex items-center gap-2">
                <span className="numeral w-6 text-xs text-muted">
                  {index + 1}
                </span>
                <input
                  inputMode="decimal"
                  value={set.weight || ""}
                  onChange={(e) => updateSet(index, "weight", e.target.value)}
                  placeholder="lbs"
                  aria-label={`Set ${index + 1} weight`}
                  className="min-w-0 flex-1 rounded-xl bg-surface-2 px-3 py-2.5 text-base outline-none placeholder:text-muted/50 focus:ring-2 focus:ring-protein/40"
                />
                <span className="text-xs text-muted">×</span>
                <input
                  inputMode="numeric"
                  value={set.reps || ""}
                  onChange={(e) => updateSet(index, "reps", e.target.value)}
                  placeholder="reps"
                  aria-label={`Set ${index + 1} reps`}
                  className="min-w-0 flex-1 rounded-xl bg-surface-2 px-3 py-2.5 text-base outline-none placeholder:text-muted/50 focus:ring-2 focus:ring-protein/40"
                />
                {sets.length > 1 && (
                  <button
                    type="button"
                    onClick={() => removeSet(index)}
                    aria-label={`Remove set ${index + 1}`}
                    className="min-h-9 min-w-9 rounded-xl text-muted active:bg-surface-2"
                  >
                    ✕
                  </button>
                )}
              </div>
            ))}
          </div>
        </>
      )}

      {error && (
        <p
          role="alert"
          className="mt-3 rounded-xl border border-over/40 bg-over/10 p-2.5 text-xs text-over"
        >
          {error}
        </p>
      )}

      <button
        type="button"
        disabled={pending || (!isRest && (!exercise.trim() || !hasValues))}
        onClick={submit}
        className="mt-3 min-h-11 w-full rounded-xl bg-protein px-4 font-semibold text-black active:opacity-80 disabled:opacity-40"
      >
        {pending ? "Logging…" : isRest ? "Log rest day" : "Log workout"}
      </button>
    </section>
  );
}
