import type { NextConfig } from "next";

const mobile = process.env.NEXT_PUBLIC_CAPACITOR === "true";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  ...(mobile ? { output: "export" as const } : {}),
  ...(mobile ? { images: { unoptimized: true } } : {}),
  // Next only supports headers in server mode. Secrets remain server-side in
  // web mode; mobile builds intentionally use NEXT_PUBLIC_ values at build time.
  ...(mobile
    ? {}
    : {
        async headers() {
          return [
            {
              source: "/manifest.json",
              headers: [{ key: "Cache-Control", value: "public, max-age=3600" }],
            },
          ];
        },
      }),
};

export default nextConfig;
