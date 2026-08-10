"use client";

import { useEffect, useMemo, useState } from "react";
import useSWR from "swr";

import type { WorkoutStatsPayload } from "@/lib/types";

const STATS_KEY = "/api/macro/workout-stats";
const MUSCLES = ["Push", "Pull", "Quads", "Hams", "Abs"] as const;
const COLORS: Record<(typeof MUSCLES)[number], string> = {
  Push: "#f87171",
  Pull: "#60a5fa",
  Quads: "#34d399",
  Hams: "#34d399",
  Abs: "#fbbf24",
};

async function fetcher(url: string): Promise<WorkoutStatsPayload> {
  const response = await fetch(url, { cache: "no-store" });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error((payload as { error?: string } | null)?.error ?? "Could not load workout stats");
  }
  return payload as WorkoutStatsPayload;
}

function Card({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <section className={`min-w-0 rounded-2xl bg-surface p-3 ${className}`}>{children}</section>;
}

function Label({ children }: { children: React.ReactNode }) {
  return <h2 className="text-[0.65rem] font-semibold uppercase tracking-[0.18em] text-muted">{children}</h2>;
}

export default function WorkoutDashboard({ revision }: { revision: number }) {
  const { data, error, isLoading, mutate } = useSWR<WorkoutStatsPayload>(STATS_KEY, fetcher, {
    refreshInterval: 15_000,
    revalidateOnFocus: true,
  });
  const [done, setDone] = useState<string[]>([]);
  const plan = data?.plan.today;
  const storageKey = useMemo(
    () => (data && plan ? `plan-done-${data.today.date}-${plan.type}` : null),
    [data, plan],
  );

  useEffect(() => {
    if (revision > 0) void mutate();
  }, [revision, mutate]);

  useEffect(() => {
    if (!storageKey) return;
    try {
      setDone(JSON.parse(localStorage.getItem(storageKey) ?? "[]") as string[]);
    } catch {
      setDone([]);
    }
  }, [storageKey]);

  const togglePlan = (exercise: string) => {
    if (!storageKey) return;
    const next = done.includes(exercise) ? done.filter((item) => item !== exercise) : [...done, exercise];
    setDone(next);
    try { localStorage.setItem(storageKey, JSON.stringify(next)); } catch { /* session state still works */ }
  };

  if (isLoading && !data) {
    return <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">{Array.from({ length: 5 }, (_, i) => <div key={i} className="h-32 animate-pulse rounded-2xl bg-surface" />)}</div>;
  }
  if (error || !data) {
    return <Card><p className="text-sm text-muted">Workout widgets are unavailable right now.</p></Card>;
  }

  const currentPlan = data.plan.today;
  const maxCoverage = Math.max(1, ...MUSCLES.map((muscle) => data.coverage.muscle_groups[muscle] ?? 0));
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
      <Card>
        <div className="flex items-center justify-between gap-2"><Label>Today</Label><span className="numeral text-[0.65rem] text-muted">{data.today.date.slice(5)}</span></div>
        <div className="mt-2 flex items-end gap-1.5"><span className="numeral text-3xl font-bold">{data.today.entries}</span><span className="pb-1 text-xs text-muted">entries</span></div>
        <div className="mt-2 flex flex-wrap gap-1">
          {data.today.exercises.slice(0, 3).map((item) => <span key={item.name} className="max-w-full truncate rounded-full bg-surface-2 px-2 py-1 text-[0.65rem]">{item.name} · {item.sets}s</span>)}
          {data.today.exercises.length === 0 && <span className="text-xs text-muted">Ready when you are</span>}
        </div>
      </Card>

      <Card>
        <div className="flex items-center justify-between gap-2"><Label>Week</Label><span className="numeral text-[0.65rem] text-muted">{data.week.week_label}</span></div>
        <div className="mt-2 flex items-end gap-1.5"><span className="numeral text-4xl font-bold">{data.week.days_logged}</span><span className="pb-1 text-xs text-muted">/ 7 days</span></div>
        <div className="mt-3 grid grid-cols-2 gap-1 text-xs"><span className="rounded-lg bg-surface-2 p-1.5"><b className="numeral">{data.week.total_sets}</b><br/><span className="text-muted">sets</span></span><span className="rounded-lg bg-surface-2 p-1.5"><b className="numeral">{data.week.streak_weeks}</b> {data.week.streak_weeks >= 2 ? "🔥" : ""}<br/><span className="text-muted">week streak</span></span></div>
      </Card>

      <Card className="col-span-2 sm:col-span-1">
        <Label>Coverage</Label>
        <div className="mt-2 space-y-1.5">{MUSCLES.map((muscle) => { const count = data.coverage.muscle_groups[muscle] ?? 0; return <div key={muscle} className="grid grid-cols-[2.8rem_1fr_1.25rem] items-center gap-1.5 text-[0.65rem]"><span>{muscle}</span><div className="h-1.5 overflow-hidden rounded-full bg-surface-2"><div className="h-full rounded-full" style={{ width: `${(count / maxCoverage) * 100}%`, backgroundColor: COLORS[muscle] }} /></div><span className="numeral rounded-full bg-surface-2 text-center">{count}</span></div>; })}</div>
        {data.coverage.untouched.length > 0 && <p className="mt-2 truncate text-[0.65rem] text-[#fbbf24]">Not hit: {data.coverage.untouched.join(", ")}</p>}
      </Card>

      <Card>
        <Label>🏆 PRs</Label>
        <div className="mt-2 flex flex-wrap gap-1.5">{data.prs.slice(0, 5).map((pr) => <span key={pr.exercise} className="max-w-full truncate rounded-full bg-surface-2 px-2 py-1 text-[0.65rem]"><b>{pr.exercise}</b> · <span className="numeral">{pr.max_weight}</span> lb</span>)}{data.prs.length === 0 && <span className="text-xs text-muted">No PRs yet</span>}</div>
      </Card>

      <Card className="col-span-1 sm:col-span-2">
        <div className="flex items-center justify-between"><Label>Today&rsquo;s plan</Label><span className="rounded-full bg-protein/10 px-2 py-0.5 text-xs font-bold text-protein">{currentPlan.type}</span></div>
        <div className="mt-2 flex flex-wrap gap-1.5">{currentPlan.exercises.map((exercise) => { const checked = done.includes(exercise); return <button key={exercise} type="button" onClick={() => togglePlan(exercise)} className={`rounded-full px-2.5 py-1 text-left text-[0.7rem] ${checked ? "bg-protein text-black line-through" : "bg-surface-2 text-text"}`}>{checked ? "✓ " : ""}{exercise}</button>; })}{currentPlan.exercises.length === 0 && <span className="text-xs text-muted">No exercises tagged yet</span>}</div>
        {data.plan.next.length > 0 && <p className="mt-2 text-[0.65rem] text-muted">Next: {data.plan.next.slice(0, 3).map((item) => item.type).join(" → ")}</p>}
      </Card>
    </div>
  );
}
