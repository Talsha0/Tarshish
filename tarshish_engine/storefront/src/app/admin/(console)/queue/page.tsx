import type { Metadata } from "next";

import { QueueView } from "@/components/admin/QueueView";

export const metadata: Metadata = { title: "Queue & jobs" };

export default function AdminQueuePage() {
  return <QueueView />;
}
