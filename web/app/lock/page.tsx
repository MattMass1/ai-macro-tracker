"use client";

import { useState } from "react";

/** Passcode screen. A lock on a personal app — one code, no accounts. */
export default function LockPage() {
  const [passcode, setPasscode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!passcode || busy) return;
    setBusy(true);
    setError(null);
    try {
      const response = await fetch("/api/auth", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(
          (payload as { error?: string } | null)?.error ?? "Wrong passcode",
        );
      }
      window.location.replace("/");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Wrong passcode");
      setPasscode("");
      setBusy(false);
    }
  }

  return (
    <main className="safe-top safe-x safe-bottom mx-auto flex min-h-dvh max-w-md flex-col items-center justify-center gap-6">
      <div className="text-center">
        <div className="mx-auto mb-4 h-14 w-14 rounded-2xl border-[5px] border-protein" />
        <h1 className="numeral text-2xl">Macros</h1>
        <p className="mt-1 text-sm text-muted">Enter your passcode</p>
      </div>

      <form onSubmit={submit} className="w-full space-y-3">
        <input
          type="password"
          inputMode="numeric"
          autoComplete="current-password"
          autoFocus
          value={passcode}
          onChange={(event) => setPasscode(event.target.value)}
          placeholder="••••"
          aria-label="Passcode"
          className="numeral min-h-14 w-full rounded-2xl bg-surface px-4 text-center text-2xl tracking-[0.4em] outline-none focus:ring-2 focus:ring-protein"
        />
        <button
          type="submit"
          disabled={busy || !passcode}
          className="min-h-14 w-full rounded-2xl bg-protein px-4 text-lg font-semibold text-black active:opacity-80 disabled:opacity-40"
        >
          {busy ? "Unlocking…" : "Unlock"}
        </button>
        {error && (
          <p role="alert" className="text-center text-sm text-over">
            {error}
          </p>
        )}
      </form>
    </main>
  );
}
