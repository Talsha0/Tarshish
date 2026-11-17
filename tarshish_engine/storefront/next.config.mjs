// FindHub storefront.
//
// The browser only ever talks to this Next.js server: `/api/*` is proxied to the FastAPI
// backend, so the storefront and its API share one domain (clean bio links, no CORS).
// API_ORIGIN is where the backend listens; 127.0.0.1 rather than "localhost" because
// Node may resolve localhost to IPv6 while uvicorn listens on IPv4.
//
// On Vercel, API_ORIGIN is the URL of the FindHub API project (for example
// https://findhub-api.vercel.app). Rewrites are fixed at build time, so the variable must be
// set for the build, and a deployment without it fails instead of proxying to 127.0.0.1.
const configuredOrigin = process.env.API_ORIGIN?.trim();
if (process.env.VERCEL && !configuredOrigin) {
  throw new Error(
    "API_ORIGIN is not set. Add it to this Vercel project's environment variables " +
      "(the URL of the FindHub API deployment, e.g. https://findhub-api.vercel.app) and redeploy.",
  );
}
const API_ORIGIN = (configuredOrigin || "http://127.0.0.1:8000").replace(/\/+$/, "");

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
    return [{ source: "/api/:path*", destination: `${API_ORIGIN}/api/:path*` }];
  },
};

export default nextConfig;
