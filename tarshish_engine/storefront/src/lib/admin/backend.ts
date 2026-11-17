// Server-side client for the FastAPI admin API (`/api/admin/*`), adding the X-Admin-Key header.

import { adminSecret } from "./session";

export function backendUrl(): string {
  const origin = process.env.BACKEND_API_URL?.trim() || process.env.API_ORIGIN?.trim() || "http://127.0.0.1:8000";
  return origin.replace(/\/+$/, "");
}

/** Call `/api/admin/<path>` on the backend. Throws only when the backend cannot be reached. */
export async function adminFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const { secret } = adminSecret();
  const headers = new Headers(init.headers);
  headers.set("X-Admin-Key", secret ?? "");
  headers.set("Accept", "application/json");
  return fetch(`${backendUrl()}/api/admin/${path.replace(/^\/+/, "")}`, {
    ...init,
    headers,
    cache: "no-store",
    signal: init.signal ?? AbortSignal.timeout(30_000),
  });
}
