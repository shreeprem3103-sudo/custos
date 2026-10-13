import type { Metadata, Viewport } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "CUSTOS · Crisis Digital Twin",
  description:
    "CUSTOS command console: predictive urban flood resilience for one city. Collect, fuse, understand, predict, simulate, recommend.",
  applicationName: "CUSTOS",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#05080d",
  colorScheme: "dark",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
