import { createHash, timingSafeEqual } from "node:crypto";
import { NextResponse } from "next/server";

import {
  SESSION_COOKIE,
  SESSION_MAX_AGE,
  sessionToken,
} from "@/lib/session";

export const runtime = "nodejs";

/** SHA-256 both sides so `timingSafeEqual` always gets equal-length buffers. */
function passcodeMatches(supplied: string, expected: string): boolean {
  const a = createHash("sha256").update(supplied).digest();
  const b = createHash("sha256").update(expected).digest();
  return timingSafeEqual(a, b);
}

export async function POST(request: Request) {
  const expected = process.env.APP_PASSCODE;
  if (!expected) {
    return NextResponse.json(
      { error: "APP_PASSCODE is not configured on the server." },
      { status: 500 },
    );
  }

  let supplied = "";
  try {
    const body = (await request.json()) as { passcode?: unknown };
    supplied = typeof body.passcode === "string" ? body.passcode : "";
  } catch {
    supplied = "";
  }

  if (!supplied || !passcodeMatches(supplied, expected)) {
    return NextResponse.json({ error: "Wrong passcode" }, { status: 401 });
  }

  const response = NextResponse.json({ ok: true });
  response.cookies.set({
    name: SESSION_COOKIE,
    value: await sessionToken(expected),
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: SESSION_MAX_AGE,
  });
  return response;
}

export async function DELETE() {
  const response = NextResponse.json({ ok: true });
  response.cookies.set({
    name: SESSION_COOKIE,
    value: "",
    httpOnly: true,
    path: "/",
    maxAge: 0,
  });
  return response;
}
