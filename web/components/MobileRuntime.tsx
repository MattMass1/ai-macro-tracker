"use client";

import { useEffect, useState } from "react";
import { apiClient } from "@/lib/apiClient";
import { isMobileBuild, isUnlocked, unlock } from "@/lib/passcodeClient";

const nativeFetch = fetch.bind(globalThis);

function responseFrom(result: unknown): Response {
  return new Response(JSON.stringify(result), { status: 200, headers: { "Content-Type": "application/json" } });
}

async function mobileFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
  if (!url.startsWith("/api/macro")) return nativeFetch(input, init);
  const path = new URL(url, window.location.origin);
  const endpoint = path.pathname.slice("/api/macro".length);
  const body = init?.body ? JSON.parse(String(init.body)) : undefined;
  const query = path.search;
  try {
    let result: unknown;
    if (endpoint === "/today") result = await apiClient.getToday();
    else if (endpoint === "/presets") result = await apiClient.getPresets();
    else if (endpoint === "/exercises") result = await apiClient.getExercises();
    else if (endpoint === "/plan") result = await apiClient.getPlan();
    else if (endpoint === "/workout-stats") result = await apiClient.getWorkoutStats();
    else if (endpoint === "/brief") result = await apiClient.getBrief(path.searchParams.get("date") ?? undefined);
    else if (endpoint.startsWith("/day/")) result = await apiClient.getDay(decodeURIComponent(endpoint.slice(5)));
    else if (endpoint.startsWith("/workouts/") && endpoint.endsWith("/last")) result = await apiClient.getLastWorkout(path.searchParams.get("exercise") ?? "");
    else if (endpoint.startsWith("/workouts/")) result = await apiClient.getWorkouts(decodeURIComponent(endpoint.slice(10)));
    else if (endpoint === "/log-preset") result = await apiClient.logPreset(body);
    else if (endpoint === "/log") result = await apiClient.logMeal(body);
    else if (endpoint === "/chat") result = await apiClient.postChat(body.message);
    else if (endpoint === "/vision-log") result = await apiClient.postVisionLog(body.image, body.meal);
    else if (endpoint === "/workout") result = await apiClient.logWorkout(body);
    else if (endpoint === "/meal/" || endpoint.startsWith("/meal/")) result = await apiClient.deleteMeal(decodeURIComponent(endpoint.slice(6)));
    else if (endpoint.startsWith("/workout/")) result = await apiClient.deleteWorkout(decodeURIComponent(endpoint.slice(9)));
    else throw new Error(`Unknown endpoint ${endpoint}${query}`);
    return responseFrom(result);
  } catch (error) {
    return new Response(JSON.stringify({ error: error instanceof Error ? error.message : "Request failed" }), { status: 502, headers: { "Content-Type": "application/json" } });
  }
}

export default function MobileRuntime({ children }: { children: React.ReactNode }) {
  const mobile = isMobileBuild();
  const [ready, setReady] = useState(!mobile);
  const [unlocked, setUnlocked] = useState(false);
  const [passcode, setPasscode] = useState("");
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!mobile) return;
    const original = window.fetch;
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => mobileFetch(input, init)) as typeof window.fetch;
    setUnlocked(isUnlocked());
    setReady(true);
    return () => { window.fetch = original; };
  }, [mobile]);

  if (!ready) return null;
  if (!mobile || unlocked) return <>{children}</>;
  return <main className="safe-top safe-x safe-bottom mx-auto flex min-h-dvh max-w-md flex-col items-center justify-center gap-6"><div className="text-center"><div className="mx-auto mb-4 h-14 w-14 rounded-2xl border-[5px] border-protein" /><h1 className="numeral text-2xl">Macros</h1><p className="mt-1 text-sm text-muted">Enter your passcode</p></div><form className="w-full space-y-3" onSubmit={(event) => { event.preventDefault(); const ok = unlock(passcode); setUnlocked(ok); setError(!ok); if (ok) setPasscode(""); }}><input type="password" inputMode="numeric" autoFocus value={passcode} onChange={(event) => setPasscode(event.target.value)} aria-label="Passcode" className="numeral min-h-14 w-full rounded-2xl bg-surface px-4 text-center text-2xl tracking-[0.4em] outline-none focus:ring-2 focus:ring-protein" /><button type="submit" disabled={!passcode} className="min-h-14 w-full rounded-2xl bg-protein px-4 text-lg font-semibold text-black disabled:opacity-40">Unlock</button>{error && <p role="alert" className="text-center text-sm text-over">Wrong passcode</p>}</form></main>;
}
