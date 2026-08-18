"use client";

import { safeEqual } from "./session";

const KEY = "macro_mobile_unlocked";

export function isMobileBuild(): boolean { return process.env.NEXT_PUBLIC_CAPACITOR === "true"; }
export function isUnlocked(): boolean { return isMobileBuild() && localStorage.getItem(KEY) === "1"; }
export function checkPasscode(passcode: string): boolean {
  const expected = process.env.NEXT_PUBLIC_APP_PASSCODE ?? "";
  return Boolean(expected) && safeEqual(passcode, expected);
}
export function unlock(passcode: string): boolean {
  if (!checkPasscode(passcode)) return false;
  localStorage.setItem(KEY, "1");
  return true;
}
export function lock(): void { localStorage.removeItem(KEY); }
