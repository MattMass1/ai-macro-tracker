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
