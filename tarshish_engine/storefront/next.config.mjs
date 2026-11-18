// FindHub storefront: the public, consumer-facing deal catalogue. Nothing administrative lives here;
// operators use the Streamlit dashboard (tarshish_engine/dashboard/app.py).
//
// The browser only ever talks to this Next.js server, so the storefront and its API share one domain
// (clean bio links, no CORS, no backend URL in the page):
//
//   /api/backend/:path*  -> BACKEND_API_URL/api/:path*   public API (products, categories, clicks)
//   /api/:path*          -> BACKEND_API_URL/api/:path*   the same, the storefront's original paths
//   /media/:path*        -> BACKEND_API_URL/media/:path*  generated FindHub promotional images (public, so Meta
//                           and TikTok can download them from the storefront's own HTTPS domain)
//
// The backend's admin API (/api/admin/*) is deliberately NOT proxied: the public domain never fronts it.
//
// BACKEND_API_URL is where the FastAPI backend listens (API_ORIGIN is still accepted). Locally
// http://127.0.0.1:8000 — 127.0.0.1 rather than "localhost" because Node may resolve localhost
// to IPv6 while uvicorn listens on IPv4. On Vercel it is the public URL of the backend (a VPS,
// a Cloudflare Tunnel, or the API's own Vercel project). Rewrites are fixed at build time, so the
// variable must be set for the build; a Vercel build without it fails instead of proxying to
// 127.0.0.1.
const configuredOrigin = (process.env.BACKEND_API_URL || process.env.API_ORIGIN)?.trim();
if (process.env.VERCEL && !configuredOrigin) {
  throw new Error(
    "BACKEND_API_URL is not set. Add it to this Vercel project's environment variables " +
      "(the public URL of the FastAPI backend, e.g. https://api.yourdomain.com) and redeploy.",
  );
}
const BACKEND = (configuredOrigin || "http://127.0.0.1:8000").replace(/\/+$/, "");

const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=()" },
];

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  turbopack: { root: import.meta.dirname },
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
  async redirects() {
    // Hebrew is the default storefront language.
    return [{ source: "/", destination: "/he", permanent: false }];
  },
  async rewrites() {
    // Only the public API and media are proxied; any admin path is excluded, so it 404s here.
    return [
      { source: "/api/backend/:path((?!admin(?:/|$)).*)", destination: `${BACKEND}/api/:path` },
      { source: "/api/:path((?!backend(?:/|$)|admin(?:/|$)).*)", destination: `${BACKEND}/api/:path` },
      { source: "/media/:path*", destination: `${BACKEND}/media/:path*` },
    ];
  },
};

export default nextConfig;
