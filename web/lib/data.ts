import { apiClient, type MacroApi } from "./apiClient";
import type {
  BriefPayload, ChatPayload, DayPayload, KnownExercise, LastWorkoutPayload,
  LogMealBody, LogPresetBody, LogWorkoutBody, Preset, WorkoutEntry,
  WorkoutPlanPayload, WorkoutStatsPayload, WorkoutsPayload, VisionPayload,
} from "./types";

const mobile = process.env.NEXT_PUBLIC_CAPACITOR === "true";

async function webRequest<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(`/api/macro${path}`, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new Error((payload as { error?: string } | null)?.error ?? `Request failed (${response.status})`);
  return payload as T;
}

const webApi: MacroApi = {
  getToday: () => webRequest("/today"), getPresets: () => webRequest("/presets"),
  logMeal: (body) => webRequest("/log", "POST", body), logPreset: (body) => webRequest("/log-preset", "POST", body),
  postChat: (message) => webRequest("/chat", "POST", { message }),
  postVisionLog: (image, meal) => webRequest("/vision-log", "POST", { image, ...(meal ? { meal } : {}) }),
  deleteMeal: (id) => webRequest(`/meal/${encodeURIComponent(id)}`, "DELETE"), getExercises: () => webRequest("/exercises"),
  getPlan: () => webRequest("/plan"), getWorkoutStats: () => webRequest("/workout-stats"),
  getWorkouts: (date) => webRequest(`/workouts/${encodeURIComponent(date)}`),
  getLastWorkout: (exercise) => webRequest(`/workouts/last?exercise=${encodeURIComponent(exercise)}`),
  logWorkout: (body) => webRequest("/workout", "POST", body), deleteWorkout: (id) => webRequest(`/workout/${encodeURIComponent(id)}`, "DELETE"),
  getDay: (date) => webRequest(`/day/${encodeURIComponent(date)}`), getBrief: (date) => webRequest(`/brief${date ? `?date=${encodeURIComponent(date)}` : ""}`),
  postBrief: (text, date) => webRequest("/brief", "POST", { text, ...(date ? { date } : {}) }),
  getChat: (message) => webRequest("/chat", "POST", { message }),
};

/** Stable client-facing barrel. Only this selection changes between builds. */
export const data: MacroApi = mobile ? apiClient : webApi;
export * from "./types";
export type { MacroApi } from "./apiClient";
