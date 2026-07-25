"use client";

import type { Macros } from "@/lib/types";

type Props = {
  totals: Macros;
  targets: Macros;
  remaining: Macros;
};

const COLORS: Record<keyof Macros, string> = {
  calories: "var(--calories)",
  protein: "var(--protein)",
  carbs: "var(--carbs)",
  fat: "var(--fat)",
};

function round(value: number): string {
  const rounded = Math.round(value * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

function Ring({
  size,
  stroke,
  consumed,
  target,
  color,
  over,
}: {
  size: number;
  stroke: number;
  consumed: number;
  target: number;
  color: string;
  over: boolean;
}) {
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const ratio = target > 0 ? Math.min(consumed / target, 1) : consumed > 0 ? 1 : 0;
  const offset = circumference * (1 - ratio);

  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      className="shrink-0"
      aria-hidden
    >
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        fill="none"
        stroke="var(--line)"
        strokeWidth={stroke}
      />
      <circle
        className="ring-arc"
        cx={size / 2}
        cy={size / 2}
        r={radius}
        fill="none"
        stroke={over ? "var(--over)" : color}
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeDasharray={circumference}
        strokeDashoffset={offset}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
      />
    </svg>
  );
}

function SmallRing({
  label,
  unit,
  consumed,
  target,
  left,
  color,
}: {
  label: string;
  unit: string;
  consumed: number;
  target: number;
  left: number;
  color: string;
}) {
  const over = left < 0;
  return (
    <div className="flex flex-col items-center rounded-2xl bg-surface px-2 py-3">
      <div className="relative">
        <Ring
          size={78}
          stroke={8}
          consumed={consumed}
          target={target}
          color={color}
          over={over}
        />
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span
            className="numeral text-lg leading-none"
            style={{ color: over ? "var(--over)" : "var(--text)" }}
          >
            {over ? round(left) : round(left)}
          </span>
          <span className="mt-0.5 text-[0.6rem] uppercase tracking-wider text-muted">
            {over ? "over" : "left"}
          </span>
        </div>
      </div>
      <div className="mt-2 text-center">
        <div className="text-xs font-semibold uppercase tracking-wider text-muted">
          {label}
        </div>
        <div className="numeral mt-0.5 text-xs text-muted">
          {round(consumed)} / {round(target)}
          {unit}
        </div>
      </div>
    </div>
  );
}

/**
 * Four readouts. Protein is the primary metric and gets the hero ring; overage
 * turns the indicator red and shows a negative number rather than clamping.
 */
export default function MacroRings({ totals, targets, remaining }: Props) {
  const proteinOver = remaining.protein < 0;

  return (
    <section className="space-y-3">
      <div className="flex items-center gap-5 rounded-3xl bg-surface p-5">
        <div className="relative">
          <Ring
            size={140}
            stroke={13}
            consumed={totals.protein}
            target={targets.protein}
            color={COLORS.protein}
            over={proteinOver}
          />
          <div className="absolute inset-0 flex flex-col items-center justify-center">
            <span
              className="numeral text-[2.4rem] leading-none"
              style={{ color: proteinOver ? "var(--over)" : "var(--protein)" }}
            >
              {round(remaining.protein)}
            </span>
            <span className="mt-1 text-[0.65rem] uppercase tracking-[0.18em] text-muted">
              {proteinOver ? "g over" : "g left"}
            </span>
          </div>
        </div>
        <div className="min-w-0">
          <div className="text-sm font-semibold uppercase tracking-[0.18em] text-protein">
            Protein
          </div>
          <div className="numeral mt-1 text-3xl">
            {round(totals.protein)}
            <span className="text-lg text-muted">/{round(targets.protein)}g</span>
          </div>
          <p className="mt-2 text-sm text-muted">
            {proteinOver
              ? `${round(Math.abs(remaining.protein))}g past target`
              : `${round(remaining.protein)}g to go today`}
          </p>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-3">
        <SmallRing
          label="Calories"
          unit=""
          consumed={totals.calories}
          target={targets.calories}
          left={remaining.calories}
          color={COLORS.calories}
        />
        <SmallRing
          label="Carbs"
          unit="g"
          consumed={totals.carbs}
          target={targets.carbs}
          left={remaining.carbs}
          color={COLORS.carbs}
        />
        <SmallRing
          label="Fat"
          unit="g"
          consumed={totals.fat}
          target={targets.fat}
          left={remaining.fat}
          color={COLORS.fat}
        />
      </div>
    </section>
  );
}
