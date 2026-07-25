"use client";

import { useEffect, useRef, useState } from "react";

import type { Meal } from "@/lib/types";

type Props = {
  meals: Meal[];
  pending: string[];
  onDelete: (meal: Meal) => void;
};

const LONG_PRESS_MS = 500;

function timeLabel(meal: Meal): string {
  if (!meal.created_time) return meal.meal;
  const date = new Date(meal.created_time);
  if (Number.isNaN(date.getTime())) return meal.meal;
  return `${meal.meal} · ${date.toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  })}`;
}

/** Newest first. Long-press (or the ✕ button) asks before deleting. */
export default function MealList({ meals, pending, onDelete }: Props) {
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

  if (meals.length === 0) {
    return (
      <section>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-[0.18em] text-muted">
          Logged
        </h2>
        <p className="rounded-2xl bg-surface p-4 text-sm text-muted">
          Nothing logged for this day yet. Tap a preset above, or text Poke what
          you ate.
        </p>
      </section>
    );
  }

  return (
    <section>
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-[0.18em] text-muted">
        Logged
      </h2>
      <ul className="space-y-2">
        {meals.map((meal) => {
          const busy = pending.includes(meal.id);
          const asking = confirming === meal.id;
          return (
            <li
              key={meal.id}
              className={`rounded-2xl bg-surface p-3 ${busy ? "opacity-50" : ""}`}
              onPointerDown={() => startPress(meal.id)}
              onPointerUp={cancelPress}
              onPointerLeave={cancelPress}
              onPointerCancel={cancelPress}
            >
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-semibold">
                    {meal.name}
                  </div>
                  <div className="numeral mt-1 text-xs text-muted">
                    {Math.round(meal.calories)} kcal ·{" "}
                    <span className="text-protein">
                      {Math.round(meal.protein)}p
                    </span>{" "}
                    · {Math.round(meal.carbs)}c · {Math.round(meal.fat)}f
                  </div>
                  <div className="mt-1 text-[0.7rem] uppercase tracking-wider text-muted">
                    {timeLabel(meal)}
                  </div>
                </div>
                {!asking && (
                  <button
                    type="button"
                    aria-label={`Delete ${meal.name}`}
                    disabled={busy}
                    onClick={() => setConfirming(meal.id)}
                    className="-m-2 flex h-11 w-11 items-center justify-center rounded-full text-muted active:bg-surface-2"
                  >
                    ✕
                  </button>
                )}
              </div>

              {asking && (
                <div className="mt-3 flex items-center justify-between gap-3 border-t border-line pt-3">
                  <span className="text-xs text-muted">Delete this entry?</span>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => setConfirming(null)}
                      className="min-h-11 rounded-xl px-4 text-sm text-muted active:bg-surface-2"
                    >
                      Keep
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setConfirming(null);
                        onDelete(meal);
                      }}
                      className="min-h-11 rounded-xl bg-over px-4 text-sm font-semibold text-black active:opacity-80"
                    >
                      Delete
                    </button>
                  </div>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
