import type { Metadata } from "next";
import { Figtree } from "next/font/google";
import Header from "@/components/Header";
import { AuthProvider } from "@/lib/auth";
import "./globals.css";

const figtree = Figtree({ subsets: ["latin"], variable: "--font-sans" });

export const metadata: Metadata = {
  title: "BidBloom",
  description: "One-of-a-kind finds, going once.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={figtree.variable}>
      <body>
        <AuthProvider>
          <Header />
          <main className="container">{children}</main>
        </AuthProvider>
      </body>
    </html>
  );
}
