import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

// Temporary sunset boundary. Reverting the sunset commit restores the web proxy.
function gone() {
  return NextResponse.json(
    {
      error: "The Macro Coach web app is temporarily disconnected. Please use the native iPhone app.",
    },
    {
      status: 410,
      headers: { "Cache-Control": "no-store" },
    },
  );
}

export const GET = gone;
export const HEAD = gone;
export const POST = gone;
export const PUT = gone;
export const DELETE = gone;
export const PATCH = gone;
export const OPTIONS = gone;
