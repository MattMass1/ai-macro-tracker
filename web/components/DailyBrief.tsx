"use client";

import useSWR from "swr";

import type { BriefPayload, WorkoutPlanPayload } from "@/lib/types";

type Props = { day: string };

async function fetcher<T>(url: string): Promise<T> {
  const response = await fetch(url, { cache: "no-store" });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(
      (payload as { error?: string } | null)?.error ??
        `Could not load today's brief (${response.status})`,
    );
  }
  return payload as T;
}

export default function DailyBrief({ day }: Props) {
  const { data: brief } = useSWR<BriefPayload>(
    `/api/macro/brief?date=${encodeURIComponent(day)}`,
    fetcher,
    { refreshInterval: 60_000, revalidateOnFocus: true },
  );
  const { data: plan } = useSWR<WorkoutPlanPayload>(
    "/api/macro/plan",
    fetcher,
    { refreshInterval: 60_000, revalidateOnFocus: true },
  );
  const today = plan?.upcoming?.[0];

  return (
    <section className="rounded-2xl bg-surface p-4">
      <h2 className="text-xs font-semibold uppercase tracking-[0.18em] text-muted">
        Today&rsquo;s brief
      </h2>
      <p
        className={`mt-2 whitespace-pre-wrap text-sm leading-relaxed ${
          brief?.text ? "text-text" : "text-muted"
        }`}
      >
        {brief?.text ?? "No brief yet today"}
      </p>
      {today && (
        <p className="mt-3 border-t border-surface-2 pt-3 text-sm text-muted">
          <span className="font-semibold text-text">Today: {today.type}</span>
          {today.exercises.length > 0 &&
            ` — ${today.exercises.map((exercise) => exercise.name).join(", ")}`}
        </p>
      )}
    </section>
  );
}
