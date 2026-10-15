/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // Phase 7 E2E: the rewrite proxy must outlive the slowest backend
  // call. Two long-running endpoints share this ceiling:
  //
  //  * Classification runner: ~44s on the isolated PostgreSQL box
  //    (verified 2026-10).
  //  * PRD generator: bounded retries — backend caps attempts at
  //    ``max(1, prd_max_retries + 1)`` (default 3) with per-call
  //    ``LLM_TIMEOUT_SECONDS=120`` (ge=1, le=600). Worst-case wall
  //    clock is therefore 120s × 3 = 360s.
  //
  // The Next.js 14.2.35 default ``experimental.proxyTimeout`` is
  // 30000ms (router-utils/proxy-request.js reads it from config);
  // both callers exceeded that ceiling and the browser saw a 500.
  // 400000ms (~6.67 min) leaves headroom for the PRD worst case
  // while still bounding truly-stuck connections. Rewrites and the
  // same-origin CORS boundary below are untouched.
  experimental: {
    proxyTimeout: 400000,
  },
  // Phase 7 work package A: the backend FastAPI service does not expose CORS.
  // We forward selected `/api/*` paths to the configured backend through
  // Next.js rewrites so the browser only talks to the same origin.
  // For a non-default backend, set NEXT_PUBLIC_API_BASE_URL.
  async rewrites() {
    const raw =
      process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, '') ?? 'http://127.0.0.1:8000';
    return [
      {
        source: '/api/:path*',
        destination: `${raw}/:path*`,
      },
    ];
  },
};

export default nextConfig;