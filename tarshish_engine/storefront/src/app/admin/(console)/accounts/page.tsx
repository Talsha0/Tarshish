import type { Metadata } from "next";

import { AccountsView } from "@/components/admin/AccountsView";

export const metadata: Metadata = { title: "Social accounts" };

export default function AdminAccountsPage() {
  return <AccountsView />;
}
