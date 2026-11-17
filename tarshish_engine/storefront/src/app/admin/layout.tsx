import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import "./admin.css";

export const metadata: Metadata = {
  title: { default: "Tarshish Admin", template: "%s · Tarshish Admin" },
  description: "Control center for harvesting, publishing and social accounts.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = { width: "device-width", initialScale: 1 };

// Applies the saved theme before the first paint (no light flash in dark mode).
const THEME_SCRIPT = `try{var t=localStorage.getItem("tarshish-admin-theme");if(t==="dark"||t==="light")document.documentElement.dataset.theme=t}catch(e){}`;

export default function AdminRootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" dir="ltr" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
