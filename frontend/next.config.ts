import type { NextConfig } from "next";

/**
 * API proxy: the SPA always talks to same-origin /api/v1/* so the browser
 * never triggers CORS. The rewrite forwards to the FastAPI backend.
 *
 * - Docker (compose network): API_PROXY_TARGET defaults to http://backend-api:8000
 * - Local dev (host): set API_PROXY_TARGET=http://localhost:8000 in .env.local
 */
const apiTarget = process.env.API_PROXY_TARGET ?? "http://backend-api:8000";

const nextConfig: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  eslint: {
    // No eslint config is shipped; Next's default lint gate would otherwise
    // block `next build` when the toolchain is absent.
    ignoreDuringBuilds: true,
  },
  async rewrites() {
    return [
      {
        source: "/api/v1/:path*",
        destination: `${apiTarget}/:path*`,
      },
    ];
  },
};

export default nextConfig;
