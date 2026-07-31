export type Macros = {
  calories: number;
  protein: number;
  carbs: number;
  fat: number;
};

export type MacroKey = keyof Macros;

export type Meal = {
  id: string;
  name: string;
  meal: string;
  calories: number;
  protein: number;
  carbs: number;
  fat: number;
  date?: string;
  created_time?: string;
};

export type Preset = {
  name: string;
  emoji: string;
  calories: number;
  protein: number;
  carbs: number;
  fat: number;
  meal: string;
  sort_order: number;
};

export type DayPayload = {
  date: string;
  day_label: string;
  totals: Macros;
  targets: Macros;
  remaining: Macros;
  meals: Meal[];
  presets?: Preset[];
  warning?: string;
};

export type LogMealBody = {
  name: string;
  calories: number;
  protein: number;
  carbs: number;
  fat: number;
  macro_source: string;
  meal?: string;
};

export type LogPresetBody = {
  preset_name: string;
  servings?: number;
  meal?: string;
};

export type WorkoutSet = {
  weight: number;
  reps: number;
};

export type WorkoutEntry = {
  id: string;
  exercise: string;
  workout_type: string[];
  muscle_group: string[];
  sets: WorkoutSet[];
  date?: string;
  created_time?: string;
};

export type KnownExercise = {
  name: string;
  workout_type: string[];
};

export const WORKOUT_TYPES = [
  "Push",
  "Pull",
  "Legs",
  "Abs",
  "Cardio",
  "Full Body",
] as const;

export type WorkoutType = (typeof WORKOUT_TYPES)[number];

export type LogWorkoutBody = {
  exercise: string;
  sets: WorkoutSet[];
  workout_type: string;
  date?: string;
};

export type WorkoutsPayload = {
  date: string;
  day_label: string;
  workouts: WorkoutEntry[];
};
