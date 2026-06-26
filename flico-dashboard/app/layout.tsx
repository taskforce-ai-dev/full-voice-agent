import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Flico Dashboard",
  description: "Call logs and details for the Flico voice agent",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen">{children}</body>
    </html>
  );
}
