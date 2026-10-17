import type { NextConfig } from "next";

// Allow the backend URL to be configured via environment variable.
// On a company laptop, set NEXT_PUBLIC_BACKEND_URL in a .env.local file
// e.g.: NEXT_PUBLIC_BACKEND_URL=http://127.0.0.1:8000
const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  allowedDevOrigins: ['127.0.0.1'],
  rewrites: async () => {
    return [
      {
        source: '/api/:path*',
        destination: `${BACKEND_URL}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
