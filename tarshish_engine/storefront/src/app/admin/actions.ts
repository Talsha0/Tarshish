"use server";

// Admin login / logout. The password is ADMIN_API_SECRET itself, checked on the server; the
// browser receives only a signed, HTTP-only session cookie (never the secret).

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { adminFetch } from "@/lib/admin/backend";
import { SESSION_COOKIE, adminSecret, checkPassword, issueToken } from "@/lib/admin/session";

export type LoginState = { error: string | null; warning?: string | null };

export async function login(_previous: LoginState, form: FormData): Promise<LoginState> {
  const { problem } = adminSecret();
  if (problem) return { error: `The admin portal is closed: ${problem}` };
  const password = String(form.get("password") ?? "");
  if (!checkPassword(password)) {
    await new Promise((resolve) => setTimeout(resolve, 600)); // slow down guessing
    return { error: "That is not the admin secret." };
  }
  const token = issueToken();
  if (!token) return { error: "Could not start a session." };
  const jar = await cookies();
  jar.set(SESSION_COOKIE, token.value, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "strict",
    path: "/",
    expires: token.expires,
  });
  const next = String(form.get("next") ?? "");
  redirect(next.startsWith("/admin") && !next.startsWith("/admin/login") ? next : "/admin");
}

export async function logout(): Promise<void> {
  const jar = await cookies();
  jar.delete(SESSION_COOKIE);
  redirect("/admin/login");
}

/** Server-side reachability check shown on the login page (no secret is revealed either way). */
export async function backendStatus(): Promise<{ ok: boolean; detail: string }> {
  try {
    const response = await adminFetch("session", { signal: AbortSignal.timeout(4000) });
    if (response.ok) return { ok: true, detail: "Backend reachable; it accepts this deployment's admin secret." };
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    return { ok: false, detail: body?.detail ?? `Backend answered HTTP ${response.status}.` };
  } catch {
    return { ok: false, detail: "Backend unreachable: check BACKEND_API_URL and that the FastAPI server is running." };
  }
}
