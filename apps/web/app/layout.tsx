import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Crypto Trading Terminal | V1.1 Mac mini 24H",
  description:
    "Personal AI crypto trading terminal — Binance & OKX Spot + USDT Perpetual, deterministic Risk Engine, structured AI TradePlans, execution reconciliation and launchd-supervised 24h operation.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-[#090D16] text-slate-100 antialiased selection:bg-emerald-500/30 selection:text-emerald-200">
        {children}
      </body>
    </html>
  );
}
