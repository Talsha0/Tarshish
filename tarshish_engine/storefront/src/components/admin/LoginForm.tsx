"use client";

import { KeyRound, LoaderCircle } from "lucide-react";
import { useActionState } from "react";

import { login, type LoginState } from "@/app/admin/actions";

export function LoginForm({ next, disabledReason }: { next: string; disabledReason: string | null }) {
  const [state, action, pending] = useActionState<LoginState, FormData>(login, { error: null });
  if (disabledReason) {
    return (
      <div className="space-y-2 text-sm">
        <p className="font-medium text-adm-bad">The admin portal is closed.</p>
        <p className="text-adm-soft">{disabledReason}</p>
        <p className="text-adm-soft">
          Set <code className="adm-mono">ADMIN_API_SECRET</code> (16+ characters, the same value as on the backend) in
          this deployment&apos;s environment variables and redeploy.
        </p>
      </div>
    );
  }
  return (
    <form action={action} className="space-y-3">
      <input type="hidden" name="next" value={next} />
      <label className="block">
        <span className="adm-label">Admin secret</span>
        <input
          name="password"
          type="password"
          required
          autoFocus
          autoComplete="current-password"
          className="adm-input"
          placeholder="ADMIN_API_SECRET"
        />
      </label>
      {state.error && <p className="text-sm text-adm-bad">{state.error}</p>}
      <button type="submit" className="adm-btn-primary w-full" disabled={pending}>
        {pending ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <KeyRound className="h-4 w-4" />}
        Log in
      </button>
      <p className="text-[11px] leading-snug text-adm-faint">
        The secret is checked on the server. Your browser keeps only a signed, HTTP-only session cookie for 12 hours.
      </p>
    </form>
  );
}
