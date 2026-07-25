/**
 * Session cookie helpers shared by `middleware.ts` (edge runtime) and the auth
 * route (node runtime), so both use the Web Crypto API rather than node:crypto.
 *
 * This is a lock on a personal app: one passcode, one cookie, no accounts.
 */

export const SESSION_COOKIE = "macro_session";
export const SESSION_MAX_AGE = 60 * 60 * 24 * 365; // one year

const SESSION_PAYLOAD = "macro-tracker-session-v1";

/**
 * A cookie value derived from the passcode. It cannot be reversed into the
 * passcode, and it changes if the passcode changes — rotating APP_PASSCODE
 * invalidates every existing session.
 */
export async function sessionToken(passcode: string): Promise<string> {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(passcode),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign(
    "HMAC",
    key,
    encoder.encode(SESSION_PAYLOAD),
  );
  return Array.from(new Uint8Array(signature))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/** Constant-time string comparison usable in both runtimes. */
export function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

export async function isValidSession(
  cookieValue: string | undefined,
  passcode: string | undefined,
): Promise<boolean> {
  if (!cookieValue || !passcode) return false;
  return safeEqual(cookieValue, await sessionToken(passcode));
}
