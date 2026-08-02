"use client";

import { useEffect, useRef, useState } from "react";

import type { WorkoutEntry } from "@/lib/types";

type Props = {
  workouts: WorkoutEntry[];
  pending: string[];
  onDelete: (workout: WorkoutEntry) => void;
};

const LONG_PRESS_MS = 500;

function setsLabel(workout: WorkoutEntry): string {
  return workout.sets
    .map((set) =>
      set.weight > 0 ? `${set.weight}×${set.reps}` : `${set.reps} reps`,
    )
    .join(" · ");
}

/** Today's logged exercises, newest first. Long-press to delete. */
export default function WorkoutList({ workouts, pending, onDelete }: Props) {
  const [confirming, setConfirming] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  const startPress = (id: string) => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setConfirming(id), LONG_PRESS_MS);
  };
  const cancelPress = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  };

  if (workouts.length === 0) {
    return (
      <section>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-[0.18em] text-muted">
          Today&rsquo;s workout
        </h2>
        <p className="rounded-2xl bg-surface p-4 text-sm text-muted">
          Nothing logged yet. Log your first exercise above — it lands in the
          Fitness Tracker, and the nightly sweep turns new maxes into PRs.
        </p>
      </section>
    );
  }

  return (
    <section>
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-[0.18em] text-muted">
        Today&rsquo;s workout · {workouts.length}
      </h2>
      <ul className="space-y-2">
        {workouts.map((workout) => {
          const busy = pending.includes(workout.id);
          const confirmingThis = confirming === workout.id;
          return (
            <li
              key={workout.id}
              onPointerDown={() => startPress(workout.id)}
              onPointerUp={cancelPress}
              onPointerLeave={cancelPress}
              onContextMenu={(e) => e.preventDefault()}
              className={`rounded-2xl bg-surface p-3 transition-[transform,box-shadow] duration-150 active:scale-[0.99] ${
                confirmingThis ? "ring-2 ring-over/60" : ""
              }`}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold">
                    {workout.exercise}
                  </div>
                  <div className="numeral mt-0.5 text-xs text-muted">
                    {setsLabel(workout)}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {(workout.workout_type ?? []).map((type) => (
                    <span
                      key={type}
                      className="rounded-full bg-surface-2 px-2 py-0.5 text-[0.65rem] font-semibold text-muted"
                    >
                      {type}
                    </span>
                  ))}
                  {confirmingThis ? (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => onDelete(workout)}
                      className="rounded-lg bg-over/20 px-2 py-1 text-xs font-semibold text-over"
                    >
                      {busy ? "…" : "Delete?"}
                    </button>
                  ) : (
                    <button
                      type="button"
                      aria-label={`Delete ${workout.exercise}`}
                      onClick={() => onDelete(workout)}
                      className="min-h-8 min-w-8 rounded-lg text-muted active:bg-surface-2"
                    >
                      ✕
                    </button>
                  )}
                </div>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
