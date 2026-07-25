"use client";

import type { Preset } from "@/lib/types";

type Props = {
  presets: Preset[];
  pending: string[];
  onLog: (preset: Preset) => void;
};

/** Tap-to-log tiles. One tap logs one serving of the preset. */
export default function PresetGrid({ presets, pending, onLog }: Props) {
  if (presets.length === 0) {
    return (
      <section>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-[0.18em] text-muted">
          Quick add
        </h2>
        <p className="rounded-2xl bg-surface p-4 text-sm text-muted">
          No presets yet. Add rows to the <strong>Meal Presets</strong>{" "}
          database in Notion, or tell Poke to &ldquo;save that as my usual
          dinner&rdquo;.
        </p>
      </section>
    );
  }

  return (
    <section>
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-[0.18em] text-muted">
        Quick add
      </h2>
      <div className="grid grid-cols-2 gap-3">
        {presets.map((preset) => {
          const busy = pending.includes(preset.name);
          return (
            <button
              key={preset.name}
              type="button"
              disabled={busy}
              onClick={() => onLog(preset)}
              className="flex min-h-[5.5rem] flex-col justify-between rounded-2xl bg-surface p-3 text-left active:bg-surface-2 disabled:opacity-50"
            >
              <div className="flex items-start justify-between gap-2">
                <span className="text-2xl leading-none">{preset.emoji}</span>
                <span className="numeral text-sm text-muted">
                  {Math.round(preset.calories)}
                </span>
              </div>
              <div>
                <div className="line-clamp-2 text-sm font-semibold">
                  {preset.name}
                </div>
                <div className="numeral mt-0.5 text-xs text-protein">
                  {Math.round(preset.protein)}g protein
                </div>
              </div>
            </button>
          );
        })}
      </div>
    </section>
  );
}
