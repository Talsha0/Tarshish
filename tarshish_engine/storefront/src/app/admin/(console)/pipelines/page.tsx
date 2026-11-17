import type { Metadata } from "next";

import { PipelinesView } from "@/components/admin/PipelinesView";

export const metadata: Metadata = { title: "Pipelines" };

export default function AdminPipelinesPage() {
  return <PipelinesView />;
}
