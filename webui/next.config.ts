import type { NextConfig } from "next"

// The backend address is injected via CE_BACKEND_URL (a service name inside Docker); default is local 8420.
const backend = process.env.CE_BACKEND_URL || "http://localhost:8420"

const nextConfig: NextConfig = {
  ...(process.env.DOCKER_BUILD ? { output: "standalone" as const } : {}),
  allowedDevOrigins: ["127.0.0.1"],
  async rewrites() {
    return [
      { source: "/api/health", destination: `${backend}/health` },
      { source: "/api/:path*", destination: `${backend}/api/:path*` },
    ]
  },
}

export default nextConfig
