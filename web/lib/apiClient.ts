"use client";

import type {
  BriefPayload, ChatPayload, DayPayload, KnownExercise, LastWorkoutPayload,
  LogMealBody, LogPresetBody, LogWorkoutBody, Preset, WorkoutEntry,
  WorkoutPlanPayload, WorkoutStatsPayload, WorkoutsPayload, VisionPayload,
} from "./types";

export class ApiClientError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiClientError";
  }
}

export interface MacroApi {
  getToday(): Promise<DayPayload>;
  getPresets(): Promise<{ presets: Preset[] }>;
  logMeal(body: LogMealBody): Promise<DayPayload>;
  logPreset(body: LogPresetBody): Promise<DayPayload>;
  postChat(message: string): Promise<ChatPayload>;
  postVisionLog(image: string, meal?: string): Promise<VisionPayload>;
  deleteMeal(pageId: string): Promise<DayPayload>;
  getExercises(): Promise<{ exercises: KnownExercise[] }>;
  getPlan(): Promise<WorkoutPlanPayload>;
  getWorkoutStats(): Promise<WorkoutStatsPayload>;
  getWorkouts(date: string): Promise<WorkoutsPayload>;
  getLastWorkout(exercise: string): Promise<LastWorkoutPayload>;
  logWorkout(body: LogWorkoutBody): Promise<WorkoutEntry>;
  deleteWorkout(pageId: string): Promise<{ deleted: string }>;
  getDay(date: string): Promise<DayPayload>;
  getBrief(date?: string): Promise<BriefPayload>;
  postBrief(text: string, date?: string): Promise<BriefPayload>;
  getChat(message: string): Promise<ChatPayload>;
}

function config(name: "NEXT_PUBLIC_MACRO_API_URL" | "NEXT_PUBLIC_APP_SHARED_TOKEN"): string {
  const value = process.env[name];
  if (!value) throw new ApiClientError(500, `${name} is not set for the mobile build.`);
  return value;
}

async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${config("NEXT_PUBLIC_MACRO_API_URL").replace(/\/+$/, "")}${path}`, {
      method,
      headers: { "Content-Type": "application/json", "X-App-Token": config("NEXT_PUBLIC_APP_SHARED_TOKEN") },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
  } catch {
    throw new ApiClientError(503, "Could not reach the macro service. Try again shortly.");
  }
  const text = await response.text();
  let payload: unknown = null;
  try { payload = text ? JSON.parse(text) : null; } catch { /* handled below */ }
  if (!response.ok) {
    throw new ApiClientError(response.status, (payload as { error?: string } | null)?.error ?? text ?? `Request to ${path} failed`);
  }
  return payload as T;
}

export const apiClient: MacroApi = {
  getToday: () => request("/api/today"),
  getPresets: () => request("/api/presets"),
  logMeal: (body) => request("/api/log", "POST", body),
  logPreset: (body) => request("/api/log-preset", "POST", body),
  postChat: (message) => request("/api/chat", "POST", { message }),
  postVisionLog: (image, meal) => request("/api/vision-log", "POST", { image, ...(meal ? { meal } : {}) }),
  deleteMeal: (pageId) => request(`/api/meal/${encodeURIComponent(pageId)}`, "DELETE"),
  getExercises: () => request("/api/exercises"),
  getPlan: () => request("/api/plan"),
  getWorkoutStats: () => request("/api/workout-stats"),
  getWorkouts: (date) => request(`/api/workouts/${encodeURIComponent(date)}`),
  getLastWorkout: (exercise) => request(`/api/workouts/last?exercise=${encodeURIComponent(exercise)}`),
  logWorkout: (body) => request("/api/workout", "POST", body),
  deleteWorkout: (pageId) => request(`/api/workout/${encodeURIComponent(pageId)}`, "DELETE"),
  getDay: (date) => request(`/api/day/${encodeURIComponent(date)}`),
  getBrief: (date) => request(`/api/brief${date ? `?date=${encodeURIComponent(date)}` : ""}`),
  postBrief: (text, date) => request("/api/brief", "POST", { text, ...(date ? { date } : {}) }),
  getChat: (message) => request("/api/chat", "POST", { message }),
};
