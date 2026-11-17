import { redirect } from "next/navigation";
import type { ReactNode } from "react";

import { AdminShell } from "@/components/admin/AdminShell";
import { isAuthenticated } from "@/lib/admin/session";

export const dynamic = "force-dynamic";

/** Login gate for every console page: no valid session cookie, no page. */
export default async function ConsoleLayout({ children }: { children: ReactNode }) {
  if (!(await isAuthenticated())) redirect("/admin/login");
  return <AdminShell>{children}</AdminShell>;
}
