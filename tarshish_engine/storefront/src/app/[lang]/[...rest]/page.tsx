// Any other address under /he or /en: show the storefront's own "not here" page.

import { notFound } from "next/navigation";

export default function UnknownPage() {
  notFound();
}
