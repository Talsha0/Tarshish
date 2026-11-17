import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { LoginForm } from "@/components/admin/LoginForm";
import { adminSecret, isAuthenticated } from "@/lib/admin/session";

import { backendStatus } from "../actions";

export const metadata: Metadata = { title: "Log in" };
export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<Record<string, string | string[] | undefined>> };

export default async function LoginPage({ searchParams }: Props) {
  if (await isAuthenticated()) redirect("/admin");
  const params = await searchParams;
  const { problem } = adminSecret();
  const backend = problem ? null : await backendStatus();
  const next = typeof params.next === "string" ? params.next : "/admin";
  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-xl bg-adm-accent text-lg font-bold text-adm-accent-ink">
            T
          </div>
          <h1 className="text-lg font-semibold">Tarshish Admin</h1>
          <p className="mt-1 text-sm text-adm-soft">Harvesting, publishing and social accounts in one place.</p>
        </div>
        <div className="adm-card p-5">
          {params.expired && <p className="mb-3 text-sm text-adm-warn">Your session expired. Log in again.</p>}
          <LoginForm next={next} disabledReason={problem} />
        </div>
        {backend && (
          <p className={`mt-3 text-center text-xs ${backend.ok ? "text-adm-soft" : "text-adm-warn"}`}>
            {backend.ok ? "● " : "▲ "}
            {backend.detail}
          </p>
        )}
      </div>
    </main>
  );
}
