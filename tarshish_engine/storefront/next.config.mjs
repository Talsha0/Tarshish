// FindHub storefront.
//
// The browser only ever talks to this Next.js server: `/api/*` is proxied to the FastAPI
// backend, so the storefront and its API share one domain (clean bio links, no CORS).
// API_ORIGIN is where the backend listens; 127.0.0.1 rather than "localhost" because
// Node may resolve localhost to IPv6 while uvicorn listens on IPv4.
const API_ORIGIN = (process.env.API_ORIGIN ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  turbopack: { root: import.meta.dirname },
  async redirects() {
    // Hebrew is the default storefront language.
    return [{ source: "/", destination: "/he", permanent: false }];
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_ORIGIN}/api/:path*` }];
  },
};

export default nextConfig;
