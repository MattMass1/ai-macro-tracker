"use client";

import { useEffect, useMemo, useState } from "react";
import useSWR from "swr";

import type { WorkoutPlanPayload } from "@/lib/types";

type Props = {
  day: string;
};

const PLAN_KEY = "/api/macro/plan";

const TYPE_EMOJI: Record<string, string> = {
  Push: "🔥",
  Pull: "🏋️",
  Legs: "🦵",
  Abs: "💪",
  Cardio: "🏃",
  "Full Body": "🧗",
};

async function fetcher(url: string): Promise<WorkoutPlanPayload> {
  const response = await fetch(url, { cache: "no-store" });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(
      (payload as { error?: string } | null)?.error ??
        `Could not load the plan (${response.status})`,
    );
  }
  return payload as WorkoutPlanPayload;
}

/**
 * Today's plan: the next workout in the rotation with its exercises as a
 * checklist, plus the following days grouped so the user can read ahead.
 * Checklist state is per-day and survives reloads (localStorage).
 */
export default function WorkoutPlan({ day }: Props) {
  const { data, error, isLoading } = useSWR<WorkoutPlanPayload>(
    PLAN_KEY,
    fetcher,
    { refreshInterval: 60_000, revalidateOnFocus: true },
  );

  const [done, setDone] = useState<string[]>([]);
  const [expanded, setExpanded] = useState<number | null>(null);

  const next = data?.upcoming?.[0];
  const storageKey = useMemo(
    () => (next ? `plan-done-${day}-${next.type}` : null),
    [day, next],
  );

  useEffect(() => {
    if (!storageKey) return;
    try {
      const raw = localStorage.getItem(storageKey);
      setDone(raw ? (JSON.parse(raw) as string[]) : []);
    } catch {
      setDone([]);
    }
  }, [storageKey]);

  const toggle = (name: string) => {
    if (!storageKey) return;
    const nextDone = done.includes(name)
      ? done.filter((entry) => entry !== name)
      : [...done, name];
    setDone(nextDone);
    try {
      localStorage.setItem(storageKey, JSON.stringify(nextDone));
    } catch {
      // storage full or unavailable — the checklist still works this session
    }
  };

  if (isLoading && !data) {
    return (
      <section className="rounded-2xl bg-surface p-4">
        <div className="h-4 w-1/3 animate-pulse rounded bg-surface-2" />
      </section>
    );
  }

  if (error || !data || !next) {
    return null;
  }

  const exercises = next.exercises;
  const completed = exercises.filter((ex) => done.includes(ex.name)).length;
  const allDone = exercises.length > 0 && completed === exercises.length;

  return (
    <section className="rounded-2xl bg-surface p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 className="text-xs font-semibold uppercase tracking-[0.18em] text-muted">
            Today&rsquo;s plan
          </h2>
          <p className="mt-1 text-lg font-bold">
            {TYPE_EMOJI[next.type] ?? "💪"} {next.type} day
          </p>
          {data.last_workout && (
            <p className="text-xs text-muted">
              Last logged: {data.last_workout}
            </p>
          )}
        </div>
        {exercises.length > 0 && (
          <span className="numeral rounded-full bg-surface-2 px-2.5 py-1 text-xs text-muted">
            {completed}/{exercises.length}
          </span>
        )}
      </div>

      {exercises.length === 0 ? (
        <p className="mt-3 text-sm text-muted">
          No exercises tagged {next.type} yet — log one from the form below
          and it&rsquo;ll join this day&rsquo;s plan.
        </p>
      ) : (
        <ul className="mt-3 space-y-1">
          {exercises.map((exercise) => {
            const checked = done.includes(exercise.name);
            return (
              <li key={exercise.name}>
                <button
                  type="button"
                  onClick={() => toggle(exercise.name)}
                  className="flex w-full items-center gap-2.5 rounded-xl px-1 py-1.5 text-left active:bg-surface-2"
                >
                  <span
                    className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-md border text-xs transition-colors ${
                      checked
                        ? "border-protein bg-protein text-black"
                        : "border-muted/40 text-transparent"
                    }`}
                  >
                    ✓
                  </span>
                  <span
                    className={`text-sm ${
                      checked ? "text-muted line-through" : "text-text"
                    }`}
                  >
                    {exercise.name}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}

      {allDone && (
        <p className="mt-3 rounded-xl bg-protein/10 px-3 py-2 text-center text-xs font-semibold text-protein">
          {next.type} day done — nice work! 💪
        </p>
      )}

      {data.core.length > 0 && (
        <details className="mt-3">
          <summary className="cursor-pointer text-xs font-semibold uppercase tracking-[0.18em] text-muted">
            Core (any day) · {data.core.length}
          </summary>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {data.core.map((exercise) => (
              <span
                key={exercise.name}
                className="rounded-full bg-surface-2 px-2.5 py-1 text-xs text-muted"
              >
                {exercise.name}
              </span>
            ))}
          </div>
        </details>
      )}

      {data.upcoming.length > 1 && (
        <div className="mt-4 border-t border-surface-2 pt-3">
          <p className="mb-2 text-[0.65rem] font-semibold uppercase tracking-[0.18em] text-muted">
            Coming up
          </p>
          <ul className="space-y-1">
            {data.upcoming.slice(1).map((item, index) => (
              <li key={`${item.type}-${index}`}>
                <button
                  type="button"
                  onClick={() =>
                    setExpanded(expanded === index + 1 ? null : index + 1)
                  }
                  className="flex w-full items-center justify-between rounded-xl px-1 py-1.5 text-left active:bg-surface-2"
                >
                  <span className="text-sm font-semibold">
                    {TYPE_EMOJI[item.type] ?? "💪"} {item.type}
                  </span>
                  <span className="text-xs text-muted">
                    {item.exercises.length} exercises{" "}
                    <span className="inline-block transition-transform">
                      {expanded === index + 1 ? "▾" : "▸"}
                    </span>
                  </span>
                </button>
                {expanded === index + 1 && (
                  <ul className="mb-1 ml-5 space-y-0.5 border-l border-surface-2 pl-3">
                    {item.exercises.map((exercise) => (
                      <li key={exercise.name} className="py-0.5 text-xs text-muted">
                        {exercise.name}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
