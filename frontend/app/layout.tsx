import type { Metadata } from "next";
import Header from "@/components/Header";
import { AuthProvider } from "@/lib/auth";
import "./globals.css";

export const metadata: Metadata = {
  title: "Live Auctions",
  description: "Real-time auctions on AWS",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <AuthProvider>
          <Header />
          <main className="container">{children}</main>
        </AuthProvider>
      </body>
    </html>
  );
}
