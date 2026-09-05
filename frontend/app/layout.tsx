import type { Metadata } from "next";

import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "Vellum",
  description:
    "Deterministic-first AI reconciliation: extract, match, and post double-entry ledgers for 50+ invoices.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-canvas font-sans antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
