/**
 * Typed fetch wrappers against the Python service.
 *
 * SERVER-SIDE ONLY. This module reads APP_SHARED_TOKEN and is imported solely by
 * route handlers, so the token never reaches the browser bundle. Every path here
 * exists on the FastMCP service.
 */

import type {
  BriefPayload,
  DayPayload,
  KnownExercise,
  LastWorkoutPayload,
  LogMealBody,
  LogPresetBody,
  LogWorkoutBody,
  Preset,
  WorkoutEntry,
  WorkoutPlanPayload,
  WorkoutStatsPayload,
  WorkoutsPayload,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

function baseUrl(): string {
  const url = process.env.MACRO_API_URL;
  if (!url) {
    throw new ApiError(
      500,
      "MACRO_API_URL is not set. Copy web/.env.example to web/.env.local and point it at the Render service.",
    );
  }
  return url.replace(/\/+$/, "");
}

function appToken(): string {
  const token = process.env.APP_SHARED_TOKEN;
  if (!token) {
    throw new ApiError(
      500,
      "APP_SHARED_TOKEN is not set. It must match the value in server/.env.",
    );
  }
  return token;
}

async function call<T>(
  path: string,
  init: { method?: string; body?: unknown } = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${baseUrl()}${path}`, {
      method: init.method ?? "GET",
      headers: {
        "Content-Type": "application/json",
        "X-App-Token": appToken(),
      },
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      cache: "no-store",
    });
  } catch {
    throw new ApiError(
      503,
      "Could not reach the macro service. If it has been idle, Render's free tier takes about 30 seconds to wake up — try again shortly.",
    );
  }

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    const message =
      (payload as { error?: string } | null)?.error ??
      text ??
      `Request to ${path} failed`;
    throw new ApiError(response.status, message);
  }
  return payload as T;
}

/** GET /api/today — the current logging day, with presets. */
export function getToday(): Promise<DayPayload> {
  return call<DayPayload>("/api/today");
}

/** GET /api/brief — the brief for a date, or the current logging day. */
export function getBrief(date?: string): Promise<BriefPayload> {
  const query = date ? `?date=${encodeURIComponent(date)}` : "";
  return call<BriefPayload>(`/api/brief${query}`);
}

/** POST /api/brief — store the brief for a date or the current logging day. */
export function postBrief(text: string, date?: string): Promise<BriefPayload> {
  return call<BriefPayload>("/api/brief", {
    method: "POST",
    body: { text, ...(date ? { date } : {}) },
  });
}

/** GET /api/day/{date} — one specific YYYY-MM-DD. */
export function getDay(date: string): Promise<DayPayload> {
  return call<DayPayload>(`/api/day/${encodeURIComponent(date)}`);
}

/** GET /api/presets — active quick-add presets. */
export function getPresets(): Promise<{ presets: Preset[] }> {
  return call<{ presets: Preset[] }>("/api/presets");
}

/** POST /api/log-preset — quick-add one (or more) servings of a saved preset. */
export function logPreset(body: LogPresetBody): Promise<DayPayload> {
  return call<DayPayload>("/api/log-preset", { method: "POST", body });
}

/** POST /api/log — manual entry with a cited macro source. */
export function logMeal(body: LogMealBody): Promise<DayPayload> {
  return call<DayPayload>("/api/log", { method: "POST", body });
}

/** DELETE /api/meal/{page_id} — archive one entry. */
export function deleteMeal(pageId: string): Promise<DayPayload> {
  return call<DayPayload>(`/api/meal/${encodeURIComponent(pageId)}`, {
    method: "DELETE",
  });
}

/** GET /api/exercises — every known exercise with its workout type tag. */
export function getExercises(): Promise<{ exercises: KnownExercise[] }> {
  return call<{ exercises: KnownExercise[] }>("/api/exercises");
}

/** GET /api/plan — the upcoming Push/Pull/Legs rotation with exercises. */
export function getPlan(): Promise<WorkoutPlanPayload> {
  return call<WorkoutPlanPayload>("/api/plan");
}

/** GET /api/workout-stats — the complete Workout dashboard snapshot. */
export function getWorkoutStats(): Promise<WorkoutStatsPayload> {
  return call<WorkoutStatsPayload>("/api/workout-stats");
}

/** GET /api/workouts/{date} — workout entries for one YYYY-MM-DD. */
export function getWorkouts(date: string): Promise<WorkoutsPayload> {
  return call<WorkoutsPayload>(`/api/workouts/${encodeURIComponent(date)}`);
}

/** GET /api/workouts/last — most recent entry for an exact exercise name. */
export function getLastWorkout(exercise: string): Promise<LastWorkoutPayload> {
  return call<LastWorkoutPayload>(
    `/api/workouts/last?exercise=${encodeURIComponent(exercise)}`,
  );
}

/** POST /api/workout — log one exercise with its sets. */
export function logWorkout(body: LogWorkoutBody): Promise<WorkoutEntry> {
  return call<WorkoutEntry>("/api/workout", { method: "POST", body });
}

/** DELETE /api/workout/{page_id} — archive one workout entry. */
export function deleteWorkout(pageId: string): Promise<{ deleted: string }> {
  return call<{ deleted: string }>(
    `/api/workout/${encodeURIComponent(pageId)}`,
    { method: "DELETE" },
  );
}
