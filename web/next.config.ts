import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Secrets stay server-side. Nothing here is prefixed NEXT_PUBLIC_, so the
  // browser bundle never receives APP_SHARED_TOKEN, APP_PASSCODE or the API url.
  async headers() {
    return [
      {
        source: "/manifest.json",
        headers: [{ key: "Cache-Control", value: "public, max-age=3600" }],
      },
    ];
  },
};

export default nextConfig;
