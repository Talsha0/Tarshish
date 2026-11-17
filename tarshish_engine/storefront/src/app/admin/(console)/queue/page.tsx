import type { Metadata } from "next";

import { QueueView } from "@/components/admin/QueueView";

export const metadata: Metadata = { title: "Content & queue" };

export default function AdminQueuePage() {
  return <QueueView />;
}
