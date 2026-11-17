// Admin session: an HTTP-only cookie holding an expiry and an HMAC of it, keyed with
// ADMIN_API_SECRET. The secret itself never leaves the server: the cookie proves the visitor
// once typed it, and the server adds the real X-Admin-Key when it calls the backend.
//
// Server-only module (node:crypto, next/headers).

import { createHmac, timingSafeEqual } from "node:crypto";
import { cookies } from "next/headers";

export const SESSION_COOKIE = "tarshish_admin";
export const SESSION_HOURS = 12;
const PLACEHOLDERS = new Set(["change_me_to_a_strong_secret", "change_me", "changeme"]);

/** The configured secret, or why the admin portal is closed. */
export function adminSecret(): { secret: string | null; problem: string | null } {
  const secret = process.env.ADMIN_API_SECRET?.trim() ?? "";
  if (!secret) return { secret: null, problem: "ADMIN_API_SECRET is not set for this deployment." };
  if (PLACEHOLDERS.has(secret.toLowerCase()))
    return { secret: null, problem: "ADMIN_API_SECRET still holds the example value; choose a real secret." };
  if (secret.length < 16) return { secret: null, problem: "ADMIN_API_SECRET must be at least 16 characters." };
  return { secret, problem: null };
}

function sign(secret: string, expires: number): string {
  return createHmac("sha256", secret).update(`tarshish-admin-session:${expires}`).digest("hex");
}

function safeEqual(a: string, b: string): boolean {
  const left = Buffer.from(a);
  const right = Buffer.from(b);
  return left.length === right.length && timingSafeEqual(left, right);
}

/** True when `candidate` is the admin secret (constant time). */
export function checkPassword(candidate: string): boolean {
  const { secret } = adminSecret();
  if (!secret) return false;
  const expected = createHmac("sha256", "pw").update(secret).digest();
  const given = createHmac("sha256", "pw").update(candidate.trim()).digest();
  return timingSafeEqual(expected, given);
}

export function issueToken(now = Date.now()): { value: string; expires: Date } | null {
  const { secret } = adminSecret();
  if (!secret) return null;
  const expires = now + SESSION_HOURS * 3600 * 1000;
  return { value: `${expires}.${sign(secret, expires)}`, expires: new Date(expires) };
}

export function verifyToken(token: string | undefined | null, now = Date.now()): boolean {
  const { secret } = adminSecret();
  if (!secret || !token) return false;
  const [rawExpires, signature] = token.split(".");
  const expires = Number(rawExpires);
  if (!Number.isFinite(expires) || expires < now || !signature) return false;
  return safeEqual(signature, sign(secret, expires));
}

/** Server components / actions: is the current visitor logged in? */
export async function isAuthenticated(): Promise<boolean> {
  const jar = await cookies();
  return verifyToken(jar.get(SESSION_COOKIE)?.value);
}
