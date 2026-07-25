/**
 * Server-side proxy to the Python service.
 *
 * The browser talks only to these handlers; `X-App-Token` is injected here, so
 * no secret ever reaches the client bundle. Paths are allow-listed rather than
 * blindly forwarded.
 */

import { NextResponse } from "next/server";

import {
  ApiError,
  deleteMeal,
  getDay,
  getPresets,
  getToday,
  logMeal,
  logPreset,
} from "@/lib/api";
import type { LogMealBody, LogPresetBody } from "@/lib/types";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type Context = { params: Promise<{ path: string[] }> };

function fail(error: unknown) {
  if (error instanceof ApiError) {
    return NextResponse.json({ error: error.message }, { status: error.status });
  }
  const message =
    error instanceof Error ? error.message : "Unexpected proxy failure";
  return NextResponse.json({ error: message }, { status: 502 });
}

const noStore = { "Cache-Control": "no-store" };

export async function GET(_request: Request, context: Context) {
  const { path } = await context.params;
  try {
    if (path.length === 1 && path[0] === "today") {
      return NextResponse.json(await getToday(), { headers: noStore });
    }
    if (path.length === 2 && path[0] === "day") {
      return NextResponse.json(await getDay(path[1]), { headers: noStore });
    }
    if (path.length === 1 && path[0] === "presets") {
      return NextResponse.json(await getPresets(), { headers: noStore });
    }
    return NextResponse.json({ error: "Unknown endpoint" }, { status: 404 });
  } catch (error) {
    return fail(error);
  }
}

export async function POST(request: Request, context: Context) {
  const { path } = await context.params;
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Body must be JSON" }, { status: 400 });
  }

  try {
    if (path.length === 1 && path[0] === "log-preset") {
      return NextResponse.json(await logPreset(body as LogPresetBody));
    }
    if (path.length === 1 && path[0] === "log") {
      return NextResponse.json(await logMeal(body as LogMealBody));
    }
    return NextResponse.json({ error: "Unknown endpoint" }, { status: 404 });
  } catch (error) {
    return fail(error);
  }
}

export async function DELETE(_request: Request, context: Context) {
  const { path } = await context.params;
  try {
    if (path.length === 2 && path[0] === "meal") {
      return NextResponse.json(await deleteMeal(path[1]));
    }
    return NextResponse.json({ error: "Unknown endpoint" }, { status: 404 });
  } catch (error) {
    return fail(error);
  }
}
