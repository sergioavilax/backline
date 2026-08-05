import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Backline",
  description:
    "Agent platform for music label operations — contracts, catalog, royalty statements.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
