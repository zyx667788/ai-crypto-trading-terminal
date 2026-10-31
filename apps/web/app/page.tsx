"use client";

import React, { useMemo, useState } from "react";
import {
  Activity,
  BarChart3,
  Bell,
  Bot,
  Cpu,
  Gauge,
  LayoutDashboard,
  LineChart,
  ListOrdered,
  Plug,
  Scale,
  Settings as SettingsIcon,
  ShieldAlert,
  Star,
  Target,
  TrendingUp,
  Wallet,
  Zap,
} from "lucide-react";
import { api, fmtAgo, fmtUsd } from "@/lib/api";
import { Badge, DataAge, StatusBadge, ToastProvider, usePoll } from "@/components/ui";
import { OverviewSection } from "@/components/sections/overview";
import { ExchangesSection } from "@/components/sections/exchanges";
import { MarketsSection } from "@/components/sections/markets";
import { AISection } from "@/components/sections/ai";
import { TradingSection } from "@/components/sections/trading";
import { RiskSection } from "@/components/sections/risk";
import { AnalyticsSection } from "@/components/sections/analytics";
import { SettingsSection } from "@/components/sections/settings";

type NavItem = { id: string; label: string; icon: React.ElementType };
type NavGroup = { id: string; label: string; items: NavItem[] };

const NAV: NavGroup[] = [
  { id: "overview", label: "", items: [{ id: "overview", label: "Overview", icon: LayoutDashboard }] },
  {
    id: "exchanges",
    label: "EXCHANGES",
    items: [{ id: "exchanges", label: "Binance / OKX", icon: Plug }],
  },
  {
    id: "markets",
    label: "MARKETS",
    items: [
      { id: "markets.overview", label: "Market Overview", icon: LineChart },
      { id: "markets.watchlist", label: "Watchlist", icon: Star },
      { id: "markets.scanner", label: "Scanner", icon: Target },
    ],
  },
  {
    id: "ai",
    label: "AI SYSTEM",
    items: [
      { id: "ai.agents", label: "Agent Monitor", icon: Bot },
      { id: "ai.decisions", label: "AI Decisions", icon: Zap },
      { id: "ai.logs", label: "Logs", icon: Activity },
    ],
  },
  {
    id: "trading",
    label: "TRADING",
    items: [
      { id: "trading.strategies", label: "Strategies", icon: TrendingUp },
      { id: "trading.positions", label: "Positions", icon: Wallet },
      { id: "trading.orders", label: "Orders", icon: ListOrdered },
      { id: "trading.fills", label: "Fills", icon: BarChart3 },
    ],
  },
  {
    id: "risk",
    label: "RISK CENTER",
    items: [
      { id: "risk.profile", label: "Risk Profile", icon: Scale },
      { id: "risk.events", label: "Risk Events", icon: ShieldAlert },
      { id: "risk.killswitch", label: "Kill Switch", icon: ShieldAlert },
    ],
  },
  {
    id: "analytics",
    label: "ANALYTICS",
    items: [
      { id: "analytics.performance", label: "Performance", icon: Gauge },
      { id: "analytics.equity", label: "Equity Curve", icon: LineChart },
      { id: "analytics.backtests", label: "Backtests", icon: Activity },
      { id: "analytics.journal", label: "Journal", icon: BarChart3 },
    ],
  },
  {
    id: "settings",
    label: "SETTINGS",
    items: [
      { id: "settings.connections", label: "API Connections", icon: Plug },
      { id: "settings.ai", label: "AI Providers", icon: Cpu },
      { id: "settings.notifications", label: "Notifications", icon: Bell },
      { id: "settings.system", label: "System", icon: SettingsIcon },
    ],
  },
];

export default function TerminalPage() {
  return (
    <ToastProvider>
      <TerminalShell />
    </ToastProvider>
  );
}

function TerminalShell() {
  const [active, setActive] = useState("overview");
  const health = usePoll(api.health, 8000);
  const portfolio = usePoll(api.portfolio, 8000);
  const automation = usePoll(api.automationStatus, 8000);

  const [group, sub] = useMemo(() => {
    const [g, s] = active.split(".");
    return [g, s] as [string, string | undefined];
  }, [active]);

  const sectionTitle = useMemo(() => {
    for (const g of NAV) for (const it of g.items) if (it.id === active) return it.label;
    return "Overview";
  }, [active]);

  const render = () => {
    switch (group) {
      case "overview":
        return <OverviewSection />;
      case "exchanges":
        return <ExchangesSection />;
      case "markets":
        return <MarketsSection view={(sub as "overview" | "watchlist" | "scanner") ?? "overview"} />;
      case "ai":
        return <AISection view={(sub as "agents" | "decisions" | "logs") ?? "agents"} />;
      case "trading":
        return <TradingSection view={(sub as "strategies" | "positions" | "orders" | "fills") ?? "strategies"} />;
      case "risk":
        return <RiskSection view={(sub as "profile" | "events" | "killswitch") ?? "profile"} />;
      case "analytics":
        return <AnalyticsSection view={(sub as "performance" | "equity" | "backtests" | "journal") ?? "performance"} />;
      case "settings":
        return <SettingsSection view={(sub as "connections" | "ai" | "notifications" | "system") ?? "connections"} />;
      default:
        return <OverviewSection />;
    }
  };

  const recovery = automation.data?.recovery;
  const online = !health.error && health.data?.status !== undefined;

  return (
    <div className="flex min-h-screen bg-terminal-bg">
      {/* ------------------------------------------------------------- sidebar */}
      <aside className="flex w-[210px] shrink-0 flex-col border-r border-terminal-border bg-[#0A0F1A]">
        <div className="border-b border-terminal-border px-4 py-3.5">
          <div className="flex items-center gap-2">
            <div className="flex h-6 w-6 items-center justify-center rounded bg-emerald-500/15 text-[11px] font-bold text-emerald-300">
              AI
            </div>
            <div className="leading-tight">
              <div className="text-[11px] font-semibold tracking-wide text-slate-100">AI TRADING</div>
              <div className="text-[9px] tracking-[0.18em] text-slate-500">TERMINAL V1.1</div>
            </div>
          </div>
        </div>

        <nav className="flex-1 overflow-y-auto py-2">
          {NAV.map((g) => (
            <div key={g.id} className="mb-2">
              {g.label && (
                <div className="px-4 pb-1 pt-1 text-[9px] font-semibold uppercase tracking-[0.16em] text-slate-600">
                  {g.label}
                </div>
              )}
              {g.items.map((item) => {
                const Icon = item.icon;
                const on = active === item.id;
                return (
                  <button
                    key={item.id}
                    onClick={() => setActive(item.id)}
                    className={`flex w-full items-center gap-2 px-4 py-[5px] text-left text-[11.5px] transition-colors ${
                      on
                        ? "border-l-2 border-emerald-400 bg-emerald-500/10 font-medium text-emerald-200"
                        : "border-l-2 border-transparent text-slate-400 hover:bg-slate-800/40 hover:text-slate-200"
                    }`}
                  >
                    <Icon size={13} className="shrink-0 opacity-80" />
                    <span className="truncate">{item.label}</span>
                    {item.id === "trading.orders" && (portfolio.data?.unknown_orders_count ?? 0) > 0 && (
                      <span className="ml-auto rounded bg-amber-500/20 px-1 text-[9px] text-amber-300">
                        {portfolio.data?.unknown_orders_count}
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="border-t border-terminal-border px-3 py-2.5 text-[9.5px] leading-relaxed text-slate-600">
          <div className="flex items-center justify-between">
            <span>API</span>
            <Badge tone={online ? "good" : "bad"}>{online ? "ONLINE" : "OFFLINE"}</Badge>
          </div>
          <div className="mt-1 flex items-center justify-between">
            <span>Live</span>
            <Badge tone={automation.data?.live_trading_enabled ? "bad" : "muted"}>
              {automation.data?.live_trading_enabled ? "ENABLED" : "OFF"}
            </Badge>
          </div>
        </div>
      </aside>

      {/* ---------------------------------------------------------------- main */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 border-b border-terminal-border bg-[#0A0F1A]/95 backdrop-blur">
          <div className="flex items-center justify-between gap-4 px-5 py-2.5">
            <div className="min-w-0">
              <h1 className="truncate text-[13px] font-semibold text-slate-100">{sectionTitle}</h1>
              <div className="mt-0.5 flex items-center gap-2 text-[10px] text-slate-500">
                <span className="font-mono">127.0.0.1:8000</span>
                <span>·</span>
                <span>环境 {automation.data?.environment?.toUpperCase() ?? "DEMO"}</span>
                {health.data?.version && (
                  <>
                    <span>·</span>
                    <span>v{health.data.version}</span>
                  </>
                )}
              </div>
            </div>

            <div className="flex items-center gap-2">
              {recovery && (
                <div className="flex items-center gap-1.5" title={recovery.new_entries_blocked_due_to.join(" | ") || "Recovery ready"}>
                  <span className="text-[9.5px] uppercase tracking-wider text-slate-600">Recovery</span>
                  <StatusBadge status={recovery.state} />
                </div>
              )}
              <div className="flex items-center gap-1.5">
                <span className="text-[9.5px] uppercase tracking-wider text-slate-600">账户同步</span>
                <DataAge iso={portfolio.data?.data_freshness.last_updated_at} stale={portfolio.data?.data_freshness.is_stale} />
              </div>
              <div className="hidden items-center gap-1.5 lg:flex">
                <span className="text-[9.5px] uppercase tracking-wider text-slate-600">Equity</span>
                <span className="font-mono text-[12px] text-slate-100">{portfolio.data ? fmtUsd(portfolio.data.total_equity) : "—"}</span>
                <span
                  className={`font-mono text-[11px] ${
                    Number(portfolio.data?.unrealized_pnl) >= 0 ? "text-emerald-300" : "text-rose-300"
                  }`}
                >
                  {portfolio.data ? `${Number(portfolio.data.unrealized_pnl) >= 0 ? "+" : ""}${fmtUsd(portfolio.data.unrealized_pnl)}` : ""}
                </span>
              </div>
            </div>
          </div>

          {recovery && !recovery.ready && (
            <div className="border-t border-amber-500/25 bg-amber-500/[0.07] px-5 py-1.5 text-[10px] text-amber-200">
              <strong>RECOVERY_MODE — 新开仓已阻断。</strong>{" "}
              {recovery.new_entries_blocked_due_to.slice(0, 2).join(" · ")}
            </div>
          )}
        </header>

        <main className="flex-1 overflow-x-hidden p-4">{render()}</main>

        <footer className="border-t border-terminal-border px-5 py-2 text-[9.5px] text-slate-600">
          所有金额使用 Decimal 精确计算 · 时间统一 UTC · AI 仅输出经 Schema 校验的 TradePlan · 所有订单必须通过确定性 Risk Engine ·
          交易所实际订单与成交记录是账户状态的最终依据 · 最后刷新 {fmtAgo(portfolio.data?.generated_at)}
        </footer>
      </div>
    </div>
  );
}
