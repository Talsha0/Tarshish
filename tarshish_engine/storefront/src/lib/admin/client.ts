// Browser-side calls to the admin API, through the authenticated Next.js proxy
// (src/app/api/backend/admin/[...path]/route.ts).

export class AdminError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "AdminError";
  }
}

function detailOf(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail))
      return detail
        .map((item) => {
          if (item && typeof item === "object" && "msg" in item) {
            const loc = (item as { loc?: unknown }).loc;
            const where = Array.isArray(loc) ? loc.filter((part) => part !== "body").join(".") : "";
            return `${where ? `${where}: ` : ""}${String((item as { msg: unknown }).msg)}`;
          }
          return String(item);
        })
        .join("; ");
  }
  return `Request failed (HTTP ${status})`;
}

export async function adminApi<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const method = init.method ?? "GET";
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (method !== "GET") headers["X-Admin-Request"] = "1";
  const response = await fetch(`/api/backend/admin/${path.replace(/^\/+/, "")}`, {
    method,
    headers,
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
    cache: "no-store",
  });
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (response.status === 401 && typeof window !== "undefined") {
    window.location.assign("/admin/login?expired=1");
  }
  if (!response.ok) throw new AdminError(detailOf(body, response.status), response.status);
  return body as T;
}

export const LANGUAGE_LABELS: Record<string, string> = { he: "HE · עברית", en: "EN · English" };

export const PLATFORM_LABELS: Record<string, string> = {
  instagram: "Instagram",
  tiktok: "TikTok",
  youtube: "YouTube Shorts",
  telegram: "Telegram",
  console: "Console",
};

export const CONTENT_TYPE_LABELS: Record<string, string> = {
  instagram_post: "Instagram post",
  instagram_story: "Instagram Story",
  tiktok_video: "TikTok video",
  youtube_short: "YouTube Short",
  telegram_post: "Telegram post",
  console_post: "Console preview",
  instagram_reel: "Instagram Reel",
  tiktok_post: "TikTok photo post",
};

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 0) return `in ${span(-seconds)}`;
  if (seconds < 45) return "just now";
  return `${span(seconds)} ago`;
}

function span(seconds: number): string {
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))} min`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h`;
  return `${Math.round(seconds / 86400)} d`;
}

export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
