import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SmartPBX Factory Console",
  description: "Private owner review surface for SmartPBX factory jobs",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
