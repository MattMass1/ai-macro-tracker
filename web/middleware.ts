import { NextResponse, type NextRequest } from "next/server";

import { SESSION_COOKIE, isValidSession } from "./lib/session";

/**
 * Passcode gate. Unauthenticated page requests are rewritten to the lock screen;
 * unauthenticated API calls get a 401. `/api/auth` is exempt — it is how you get
 * a session in the first place.
 */
export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Temporary sunset bypass: the static notice and disabled proxy need no passcode.
  if (pathname === "/" || pathname.startsWith("/api/macro/")) {
    return NextResponse.next();
  }

  if (pathname.startsWith("/api/auth")) {
    return NextResponse.next();
  }

  const cookie = request.cookies.get(SESSION_COOKIE)?.value;
  const authorized = await isValidSession(cookie, process.env.APP_PASSCODE);

  if (authorized) {
    // Never let the lock screen show once unlocked.
    if (pathname === "/lock") {
      return NextResponse.redirect(new URL("/", request.url));
    }
    return NextResponse.next();
  }

  if (pathname.startsWith("/api/")) {
    return NextResponse.json({ error: "Locked" }, { status: 401 });
  }

  if (pathname === "/lock") {
    return NextResponse.next();
  }

  return NextResponse.rewrite(new URL("/lock", request.url));
}

export const config = {
  // Everything except Next internals and the static icons/manifest, which iOS
  // must be able to read before the app is unlocked.
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|manifest.json|sw.js|icon-192.png|icon-512.png|apple-touch-icon.png).*)",
  ],
};
