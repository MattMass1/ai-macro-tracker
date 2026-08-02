"use client";

import useSWR from "swr";

import type { BriefPayload, WorkoutPlanPayload } from "@/lib/types";

type Props = { day: string };

type Verdict = {
  label: "FULL SEND" | "MODERATE" | "EASY" | "READY";
  color: string;
};

type Metric = {
  icon: string;
  label: string;
  value: string;
  unit: string;
};

const VERDICTS: Verdict[] = [
  { label: "FULL SEND", color: "#34d399" },
  { label: "MODERATE", color: "#fbbf24" },
  { label: "EASY", color: "#f87171" },
];

const NEUTRAL_VERDICT: Verdict = { label: "READY", color: "#9ca3af" };

const METRIC_PATTERNS = {
  rhr: /\bRHR\s*[:=]?\s*(\d+)\s*(?:bpm)?\b/i,
  hrv: /\bHRV\s*[:=]?\s*(\d+)\s*(?:ms)?\b/i,
  sleep: /\bsleep(?:\s+duration)?\s*[:=]?\s*([\d.]+)\s*(?:h|hrs?|hours?)\b/i,
};

function getVerdict(text: string): Verdict {
  if (/🟢|\bgreen\b|\bfull[\s-]?send\b/i.test(text)) return VERDICTS[0];
  if (/🟡|\byellow\b|\bmoderate\b/i.test(text)) return VERDICTS[1];
  if (/🔴|\bred\b|\beasy\b/i.test(text)) return VERDICTS[2];
  return NEUTRAL_VERDICT;
}

function getMetrics(text: string): Metric[] {
  const rhr = text.match(METRIC_PATTERNS.rhr)?.[1];
  const hrv = text.match(METRIC_PATTERNS.hrv)?.[1];
  const sleep = text.match(METRIC_PATTERNS.sleep)?.[1];

  return [
    rhr && { icon: "❤️", label: "RHR", value: rhr, unit: "bpm" },
    hrv && { icon: "📊", label: "HRV", value: hrv, unit: "ms" },
    sleep && { icon: "😴", label: "Sleep", value: sleep, unit: "h" },
  ].filter((metric): metric is Metric => Boolean(metric));
}

function getBodyText(text: string): string {
  return text
    .replace(/^\s*(?:🟢|🟡|🔴)?\s*(?:green|yellow|red|full[\s-]?send|moderate|easy)\s*(?:—|-|:)?\s*/i, "")
    .replace(new RegExp(METRIC_PATTERNS.rhr.source, "gi"), "")
    .replace(new RegExp(METRIC_PATTERNS.hrv.source, "gi"), "")
    .replace(new RegExp(METRIC_PATTERNS.sleep.source, "gi"), "")
    .replace(/(?:\s*[·|]\s*)+/g, " · ")
    .replace(/(?:^|\s)[·|]\s*/g, " ")
    .replace(/\s+([.,;:])/g, "$1")
    .replace(/(?:^|\s)[.,;]+\s*/g, " ")
    .replace(/([.!?])\1+/g, "$1")
    .replace(/\s{2,}/g, " ")
    .trim();
}

function workoutColor(type: string): string {
  switch (type.toLowerCase()) {
    case "push":
      return "#f87171";
    case "pull":
      return "#60a5fa";
    case "legs":
      return "#34d399";
    case "abs":
      return "#fbbf24";
    default:
      return "#9ca3af";
  }
}

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
  const text = brief?.text;
  const verdict = getVerdict(text ?? "");
  const metrics = getMetrics(text ?? "");
  const body = text ? getBodyText(text) : "";

  return (
    <section className="rounded-2xl bg-surface p-4">
      <div className="flex items-center gap-2">
        {text && (
          <span
            className="rounded-full px-2.5 py-1 text-[0.65rem] font-bold tracking-wide"
            style={{ color: verdict.color, backgroundColor: `${verdict.color}1a` }}
          >
            {verdict.label}
          </span>
        )}
        <h2 className="text-xs font-semibold uppercase tracking-[0.18em] text-muted">
          Today&rsquo;s brief
        </h2>
      </div>

      {!text && <p className="mt-3 text-sm text-muted">No brief yet today</p>}

      {metrics.length > 0 && (
        <div className="mt-3 grid grid-cols-3 gap-1.5">
          {metrics.map((metric) => (
            <div key={metric.label} className="min-w-0 rounded-xl bg-surface-2 p-2 text-center">
              <div className="text-sm" aria-hidden="true">{metric.icon}</div>
              <div className="mt-0.5 whitespace-nowrap">
                <span className="numeral text-xl font-bold text-text">{metric.value}</span>
                <span className="ml-0.5 text-[0.6rem] text-muted">{metric.unit}</span>
              </div>
              <div className="text-[0.6rem] text-muted">{metric.label}</div>
            </div>
          ))}
        </div>
      )}

      {body && <p className="mt-3 line-clamp-2 text-sm leading-relaxed text-muted">{body}</p>}

      {today && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5 border-t border-surface-2 pt-3">
          <span className="mr-0.5 text-xs font-semibold text-muted">Today:</span>
          <span
            className="rounded-full px-2.5 py-1 text-[0.7rem] font-bold"
            style={{ color: workoutColor(today.type), backgroundColor: `${workoutColor(today.type)}1a` }}
          >
            {today.type}
          </span>
          {today.exercises.map((exercise) => (
            <span key={exercise.name} className="max-w-full truncate rounded-full bg-surface-2 px-2.5 py-1 text-[0.7rem] text-text">
              {exercise.name}
            </span>
          ))}
        </div>
      )}
    </section>
  );
}
