import type { NextConfig } from "next";

// All browser traffic goes to same-origin /api/* and /media/* by default,
// which Next proxies to the FastAPI backend. This lets a single public tunnel
// (e.g. cloudflared/ngrok pointed at :3000) serve the whole app to a phone.
const BACKEND_URL = (process.env.BACKEND_URL || "http://localhost:8000").replace(/\/$/, "");

const nextConfig: NextConfig = {
  devIndicators: false,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${BACKEND_URL}/api/:path*` },
      { source: "/media/:path*", destination: `${BACKEND_URL}/media/:path*` },
    ];
  },
  // Detection/evaluation can take a while (Gemini + cutouts)
  experimental: {
    proxyTimeout: 180_000,
  },
  // Allow the dev server to be reached through common tunnels
  allowedDevOrigins: [
    "*.trycloudflare.com",
    "*.ngrok-free.app",
    "*.ngrok.app",
    "*.ngrok.io",
    "*.loca.lt",
    "**.devtunnels.ms",
    "*.local",
    "192.168.*.*",
  ],
};

export default nextConfig;
