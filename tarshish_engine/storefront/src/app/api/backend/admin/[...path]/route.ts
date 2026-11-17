// Authenticated proxy from the admin portal to the FastAPI admin API.
//
//   browser ──/api/backend/admin/<path>──► this route ──X-Admin-Key──► BACKEND_API_URL/api/admin/<path>
//
// The browser sends only its HTTP-only session cookie; the admin secret is added here, on the
// server. Mutating requests must also carry `X-Admin-Request: 1`, a header a cross-site form or
// image cannot set (defence in depth next to the SameSite=Strict cookie).

import type { NextRequest } from "next/server";

import { adminFetch, backendUrl } from "@/lib/admin/backend";
import { SESSION_COOKIE, verifyToken } from "@/lib/admin/session";

export const dynamic = "force-dynamic";

type Context = { params: Promise<{ path: string[] }> };

function json(status: number, detail: string): Response {
  return Response.json({ detail }, { status, headers: { "Cache-Control": "no-store" } });
}

async function proxy(request: NextRequest, context: Context): Promise<Response> {
  if (!verifyToken(request.cookies.get(SESSION_COOKIE)?.value)) {
    return json(401, "Your admin session has expired. Log in again.");
  }
  const method = request.method.toUpperCase();
  if (method !== "GET" && request.headers.get("x-admin-request") !== "1") {
    return json(403, "Missing X-Admin-Request header.");
  }
  const { path } = await context.params;
  if (path.some((part) => part === ".." || part === ".")) return json(400, "Invalid path.");
  const target = path.map(encodeURIComponent).join("/") + request.nextUrl.search;
  let response: Response;
  try {
    response = await adminFetch(target, {
      method,
      headers: { "Content-Type": "application/json" },
      body: method === "GET" ? undefined : (await request.text()) || undefined,
    });
  } catch (error) {
    const reason = error instanceof Error && error.name === "TimeoutError" ? "timed out" : "is unreachable";
    const host = (() => {
      try {
        return new URL(backendUrl()).host;
      } catch {
        return "BACKEND_API_URL";
      }
    })();
    return json(502, `The backend ${reason} (${host}). Is the FastAPI server running and BACKEND_API_URL right?`);
  }
  const body = await response.text();
  return new Response(body, {
    status: response.status,
    headers: {
      "Content-Type": response.headers.get("content-type") ?? "application/json",
      "Cache-Control": "no-store",
    },
  });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
