"use client";

type Props = {
  dayLabel: string;
  mealCount: number;
  updatedAt: number | null;
  refreshing: boolean;
};

function agoLabel(updatedAt: number | null): string {
  if (!updatedAt) return "";
  const seconds = Math.max(0, Math.round((Date.now() - updatedAt) / 1000));
  if (seconds < 20) return "just now";
  if (seconds < 90) return `${seconds}s ago`;
  return `${Math.round(seconds / 60)}m ago`;
}

/**
 * Names the day being shown. Between midnight and 4am the logging day and the
 * calendar date disagree, so this never says "Today".
 */
export default function DayHeader({
  dayLabel,
  mealCount,
  updatedAt,
  refreshing,
}: Props) {
  return (
    <header className="flex items-end justify-between gap-3">
      <div>
        <h1 className="numeral text-[1.75rem] leading-tight">{dayLabel}</h1>
        <p className="mt-1 text-sm text-muted">
          {mealCount === 0
            ? "Nothing logged yet"
            : `${mealCount} ${mealCount === 1 ? "entry" : "entries"} logged`}
        </p>
      </div>
      <div className="flex items-center gap-2 pb-1 text-xs text-muted">
        <span
          aria-hidden
          className={`h-2 w-2 rounded-full ${
            refreshing ? "bg-protein" : "bg-line"
          }`}
        />
        <span>{refreshing ? "syncing" : agoLabel(updatedAt)}</span>
      </div>
    </header>
  );
}
