"use client";

import { useCallback, useEffect, useState } from "react";
import useSWR from "swr";

import DayHeader from "@/components/DayHeader";
import MacroRings from "@/components/MacroRings";
import MealList from "@/components/MealList";
import PresetGrid from "@/components/PresetGrid";
import type { DayPayload, Macros, Meal, Preset } from "@/lib/types";

const TODAY_KEY = "/api/macro/today";
const POLL_MS = 15_000;

async function fetcher(url: string): Promise<DayPayload> {
  const response = await fetch(url, { cache: "no-store" });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(
      (payload as { error?: string } | null)?.error ??
        `Could not reach the macro service (${response.status})`,
    );
  }
  return payload as DayPayload;
}

const ZERO: Macros = { calories: 0, protein: 0, carbs: 0, fat: 0 };

function addMacros(a: Macros, b: Macros, sign: 1 | -1): Macros {
  return {
    calories: Math.round((a.calories + sign * b.calories) * 100) / 100,
    protein: Math.round((a.protein + sign * b.protein) * 100) / 100,
    carbs: Math.round((a.carbs + sign * b.carbs) * 100) / 100,
    fat: Math.round((a.fat + sign * b.fat) * 100) / 100,
  };
}

function withMeal(day: DayPayload, meal: Meal, sign: 1 | -1): DayPayload {
  const totals = addMacros(day.totals, meal, sign);
  return {
    ...day,
    totals,
    remaining: addMacros(day.remaining, meal, sign === 1 ? -1 : 1),
    meals:
      sign === 1
        ? [meal, ...day.meals]
        : day.meals.filter((entry) => entry.id !== meal.id),
  };
}

/**
 * Log/delete responses carry the day's numbers but not always the preset list
 * (the MCP tools do not pay for that query). Keep the tiles we already have.
 */
function keepPresets(payload: DayPayload, previous: DayPayload): DayPayload {
  return payload.presets ? payload : { ...payload, presets: previous.presets };
}

export default function TodayPage() {
  const { data, error, isValidating, mutate } = useSWR<DayPayload>(
    TODAY_KEY,
    fetcher,
    {
      refreshInterval: POLL_MS,
      revalidateOnFocus: true,
      revalidateOnReconnect: true,
      keepPreviousData: true,
    },
  );

  const [pendingPresets, setPendingPresets] = useState<string[]>([]);
  const [pendingMeals, setPendingMeals] = useState<string[]>([]);
  const [actionError, setActionError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [, forceTick] = useState(0);

  useEffect(() => {
    if (data) setUpdatedAt(Date.now());
  }, [data]);

  // Keeps the "x ago" label honest between polls.
  useEffect(() => {
    const id = setInterval(() => forceTick((n) => n + 1), 10_000);
    return () => clearInterval(id);
  }, []);

  const logPreset = useCallback(
    async (preset: Preset) => {
      if (!data) return;
      setActionError(null);
      setPendingPresets((names) => [...names, preset.name]);

      const optimistic = withMeal(
        data,
        {
          id: `optimistic-${preset.name}-${Date.now()}`,
          name: preset.name,
          meal: preset.meal,
          calories: preset.calories,
          protein: preset.protein,
          carbs: preset.carbs,
          fat: preset.fat,
          created_time: new Date().toISOString(),
        },
        1,
      );
      await mutate(optimistic, { revalidate: false });

      try {
        const response = await fetch("/api/macro/log-preset", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ preset_name: preset.name, servings: 1 }),
        });
        const payload = await response.json().catch(() => null);
        if (!response.ok) {
          throw new Error(
            (payload as { error?: string } | null)?.error ??
              `Logging ${preset.name} failed (${response.status})`,
          );
        }
        await mutate(keepPresets(payload as DayPayload, data), {
          revalidate: false,
        });
      } catch (caught) {
        // Roll back to the pre-tap numbers, then say why.
        await mutate(data, { revalidate: false });
        setActionError(
          caught instanceof Error ? caught.message : "Could not log that preset",
        );
      } finally {
        setPendingPresets((names) =>
          names.filter((name) => name !== preset.name),
        );
      }
    },
    [data, mutate],
  );

  const deleteMeal = useCallback(
    async (meal: Meal) => {
      if (!data) return;
      setActionError(null);
      setPendingMeals((ids) => [...ids, meal.id]);
      await mutate(withMeal(data, meal, -1), { revalidate: false });

      try {
        const response = await fetch(
          `/api/macro/meal/${encodeURIComponent(meal.id)}`,
          { method: "DELETE" },
        );
        const payload = await response.json().catch(() => null);
        if (!response.ok) {
          throw new Error(
            (payload as { error?: string } | null)?.error ??
              `Deleting that entry failed (${response.status})`,
          );
        }
        await mutate(keepPresets(payload as DayPayload, data), {
          revalidate: false,
        });
      } catch (caught) {
        await mutate(data, { revalidate: false });
        setActionError(
          caught instanceof Error ? caught.message : "Could not delete that entry",
        );
      } finally {
        setPendingMeals((ids) => ids.filter((id) => id !== meal.id));
      }
    },
    [data, mutate],
  );

  if (!data) {
    return (
      <main className="safe-top safe-x safe-bottom mx-auto flex min-h-dvh max-w-md flex-col justify-center gap-4">
        {error ? (
          <div className="rounded-2xl bg-surface p-5">
            <h1 className="text-lg font-semibold">Can&rsquo;t load today</h1>
            <p className="mt-2 text-sm text-muted">{String(error.message)}</p>
            <p className="mt-2 text-xs text-muted">
              If the server has been idle, Render&rsquo;s free tier takes about
              30 seconds to wake up.
            </p>
            <button
              type="button"
              onClick={() => mutate()}
              className="mt-4 min-h-11 w-full rounded-xl bg-protein px-4 font-semibold text-black active:opacity-80"
            >
              Try again
            </button>
          </div>
        ) : (
          <div className="animate-pulse space-y-3">
            <div className="h-8 w-2/3 rounded-lg bg-surface" />
            <div className="h-44 rounded-3xl bg-surface" />
            <div className="grid grid-cols-3 gap-3">
              <div className="h-32 rounded-2xl bg-surface" />
              <div className="h-32 rounded-2xl bg-surface" />
              <div className="h-32 rounded-2xl bg-surface" />
            </div>
          </div>
        )}
      </main>
    );
  }

  return (
    <main className="safe-top safe-x safe-bottom mx-auto flex min-h-dvh max-w-md flex-col gap-5">
      <DayHeader
        dayLabel={data.day_label}
        mealCount={data.meals.length}
        updatedAt={updatedAt}
        refreshing={isValidating}
      />

      <MacroRings
        totals={data.totals ?? ZERO}
        targets={data.targets ?? ZERO}
        remaining={data.remaining ?? ZERO}
      />

      {(actionError || error) && (
        <p
          role="alert"
          className="rounded-2xl border border-over/40 bg-over/10 p-3 text-sm text-over"
        >
          {actionError ?? String(error?.message ?? "")}
        </p>
      )}

      <PresetGrid
        presets={data.presets ?? []}
        pending={pendingPresets}
        onLog={logPreset}
      />

      <MealList
        meals={data.meals}
        pending={pendingMeals}
        onDelete={deleteMeal}
      />

      <p className="pt-2 text-center text-[0.7rem] text-muted">
        Day rolls over at 4am. Text Poke to log anything that isn&rsquo;t a
        preset.
      </p>
    </main>
  );
}
