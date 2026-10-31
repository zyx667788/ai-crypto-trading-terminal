"use client";

import React, { useState } from "react";
import {
  api,
  fmtAgo,
  fmtNum,
  fmtSigned,
  fmtUsd,
  fmtUtc,
  type EquityPoint,
  type PortfolioSummary,
} from "@/lib/api";
import { Badge, Button, Card, DataAge, Empty, ErrorLine, Stat, StatusBadge, Table, useAction, usePoll } from "@/components/ui";
import { EquityChart } from "@/components/charts";

export function OverviewSection() {
  const portfolio = usePoll<PortfolioSummary>(api.portfolio, 5000);
  const curve = usePoll<EquityPoint[]>(api.equityCurve, 15000);
  const automation = usePoll(api.automationStatus, 5000);
  const orders = usePoll(() => api.orders(), 5000);
  const events = usePoll(() => api.systemEvents(15), 8000);
  const { busy, run } = useAction();
  const [cycle, setCycle] = useState<Record<string, unknown> | null>(null);

  const p = portfolio.data;
  const equityPoints =
    curve.data?.map((pt) => ({ at: pt.observed_at, value: Number(pt.equity) })).filter((pt) => Number.isFinite(pt.value)) ?? [];

  const runCycle = async () => {
    const result = (await run("cycle", () => api.runCycle(), "完整 AI 流水线已执行")) as Record<string, unknown> | null;
    if (result) setCycle(result);
    portfolio.reload();
    orders.reload();
  };

  return (
    <div className="space-y-4">
      {portfolio.error && <ErrorLine error={portfolio.error} onRetry={portfolio.reload} />}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4 xl:grid-cols-6">
        <Stat
          label={`总权益 (${p?.currency ?? "USDT"})`}
          value={p ? fmtUsd(p.total_equity) : "—"}
          sub={p ? <DataAge iso={p.data_freshness.last_updated_at} stale={p.data_freshness.is_stale} /> : "加载中"}
        />
        <Stat
          label="已实现盈亏"
          value={p ? fmtSigned(p.realized_pnl) : "—"}
          tone={Number(p?.realized_pnl) >= 0 ? "good" : "bad"}
          sub="Realized PnL"
        />
        <Stat
          label="未实现盈亏"
          value={p ? fmtSigned(p.unrealized_pnl) : "—"}
          tone={Number(p?.unrealized_pnl) >= 0 ? "good" : "bad"}
          sub="Unrealized PnL"
        />
        <Stat label="手续费" value={p ? `-${fmtUsd(p.fees_paid)}` : "—"} tone="warn" sub="Fees paid" />
        <Stat
          label="资金费率收支"
          value={p ? fmtSigned(p.funding_pnl) : "—"}
          tone={Number(p?.funding_pnl) >= 0 ? "good" : "bad"}
          sub="Funding PnL"
        />
        <Stat
          label="净入金/出金"
          value={p ? fmtUsd(p.net_transfers) : "—"}
          sub="不计入交易利润"
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card
          title="账户与敞口"
          subtitle="权益 / 可用 / 保证金 / 名义敞口"
          right={p ? <Badge tone={p.data_freshness.is_stale ? "warn" : "good"}>{p.data_freshness.label}</Badge> : null}
          className="xl:col-span-2"
        >
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="可用余额" value={p ? fmtUsd(p.available_balance) : "—"} />
            <Stat label="保证金余额" value={p ? fmtUsd(p.margin_balance) : "—"} />
            <Stat label="名义敞口" value={p ? fmtUsd(p.notional_exposure) : "—"} />
            <Stat label="现货估值" value={p ? fmtUsd(p.spot_valuation) : "—"} />
          </div>

          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            {(["binance", "okx"] as const).map((ex) => {
              const bucket = p?.per_exchange?.[ex];
              return (
                <div key={ex} className="rounded-md border border-terminal-border/70 bg-terminal-panel/50 p-3">
                  <div className="mb-2 flex items-center justify-between">
                    <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-300">{ex}</span>
                    <Badge tone={bucket ? "good" : "warn"}>{bucket ? "CONNECTED" : "NO DATA"}</Badge>
                  </div>
                  <div className="grid grid-cols-2 gap-x-3 gap-y-1.5 font-mono text-[11px]">
                    <span className="text-slate-500">Equity</span>
                    <span className="text-right text-slate-200">{bucket ? fmtUsd(bucket.equity) : "—"}</span>
                    <span className="text-slate-500">Available</span>
                    <span className="text-right text-slate-300">{bucket ? fmtUsd(bucket.available) : "—"}</span>
                    <span className="text-slate-500">Margin</span>
                    <span className="text-right text-slate-300">{bucket ? fmtUsd(bucket.margin) : "—"}</span>
                    <span className="text-slate-500">Unrealized</span>
                    <span className={`text-right ${Number(bucket?.unrealized_pnl) >= 0 ? "text-emerald-300" : "text-rose-300"}`}>
                      {bucket ? fmtSigned(bucket.unrealized_pnl) : "—"}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>

          <p className="mt-3 text-[10px] leading-relaxed text-slate-500">{p?.transfers_note}</p>
        </Card>

        <div className="space-y-4">
          <Card
            title="AI 自动化流水线"
            subtitle={`环境 ${automation.data?.environment ?? "—"} · Live ${automation.data?.live_trading_enabled ? "ON" : "OFF"}`}
            right={
              automation.data?.recovery ? (
                <StatusBadge status={automation.data.recovery.state} />
              ) : null
            }
          >
            <div className="space-y-2">
              <div className="flex flex-wrap gap-1.5">
                {(automation.data?.pipeline ?? []).map((stage, i) => (
                  <span key={stage} className="text-[10px] text-slate-500">
                    <span className="rounded bg-slate-800/70 px-1.5 py-0.5 font-mono text-slate-400">{i + 1}. {stage}</span>
                    {i < (automation.data?.pipeline.length ?? 0) - 1 && <span className="px-0.5 text-slate-700">→</span>}
                  </span>
                ))}
              </div>
              <div className="flex items-center justify-between text-[11px]">
                <span className="text-slate-500">上次循环</span>
                <span className="font-mono text-slate-300">{fmtAgo(automation.data?.last_cycle_at)}</span>
              </div>
              {automation.data?.recovery && !automation.data.recovery.ready && (
                <div className="rounded border border-amber-500/40 bg-amber-500/10 p-2 text-[10px] text-amber-200">
                  <strong>新开仓已阻断：</strong>
                  {automation.data.recovery.new_entries_blocked_due_to.slice(0, 3).map((r, i) => (
                    <div key={i}>· {r}</div>
                  ))}
                </div>
              )}
              <div className="flex flex-wrap gap-2 pt-1">
                <Button variant="primary" size="sm" onClick={runCycle} loading={busy === "cycle"}>
                  运行一次完整 AI 流水线
                </Button>
                <Button size="sm" loading={busy === "pause"} onClick={() => run("pause", api.pauseAutomation, "已暂停新开仓")}>
                  暂停新开仓
                </Button>
              </div>
              {cycle && (
                <div className="mt-2 space-y-1 rounded border border-terminal-border bg-terminal-panel/70 p-2">
                  {(cycle.steps as Array<{ step: string; ok: boolean; detail: unknown }> | undefined)?.map((s) => (
                    <div key={s.step} className="flex items-center justify-between gap-2 text-[10px]">
                      <span className="text-slate-400">{s.step}</span>
                      <Badge tone={s.ok ? "good" : "bad"}>{s.ok ? "OK" : "FAIL"}</Badge>
                    </div>
                  ))}
                  {Boolean(cycle.blocked_reason) && (
                    <div className="pt-1 text-[10px] text-amber-300">阻断原因: {String(cycle.blocked_reason)}</div>
                  )}
                </div>
              )}
            </div>
          </Card>

          <Card title="运行状态" subtitle="策略与智能体">
            <div className="grid grid-cols-2 gap-2 text-[11px]">
              <div className="rounded border border-terminal-border/60 bg-terminal-panel/50 p-2">
                <div className="text-slate-500">运行策略</div>
                <div className="mt-0.5 font-mono text-slate-200">{p?.active_strategies.length ?? 0}</div>
              </div>
              <div className="rounded border border-terminal-border/60 bg-terminal-panel/50 p-2">
                <div className="text-slate-500">活跃信号</div>
                <div className="mt-0.5 font-mono text-slate-200">{p?.active_signals_count ?? 0}</div>
              </div>
              <div className="rounded border border-terminal-border/60 bg-terminal-panel/50 p-2">
                <div className="text-slate-500">持仓 / 挂单</div>
                <div className="mt-0.5 font-mono text-slate-200">
                  {p?.open_positions_count ?? 0} / {p?.open_orders_count ?? 0}
                </div>
              </div>
              <div className="rounded border border-terminal-border/60 bg-terminal-panel/50 p-2">
                <div className="text-slate-500">UNKNOWN 订单</div>
                <div className={`mt-0.5 font-mono ${(p?.unknown_orders_count ?? 0) > 0 ? "text-amber-300" : "text-slate-200"}`}>
                  {p?.unknown_orders_count ?? 0}
                </div>
              </div>
            </div>
            <div className="mt-2 flex items-center justify-between text-[10px] text-slate-500">
              <span>Agent: {p?.agent_status.last_run_type ?? "IDLE"}</span>
              <StatusBadge status={p?.agent_status.last_run_status ?? "IDLE"} />
            </div>
          </Card>
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card title="净值曲线 (Equity Curve)" subtitle="来自 equity_snapshots 快照" className="xl:col-span-2">
          {equityPoints.length >= 2 ? <EquityChart points={equityPoints} /> : <Empty>快照点不足。运行一次流水线即可生成。</Empty>}
        </Card>

        <Card title="数据新鲜度" subtitle="所有面板均标注数据时间">
          <ul className="space-y-2 text-[11px]">
            <li className="flex items-center justify-between">
              <span className="text-slate-500">账户同步</span>
              <span className="font-mono text-slate-300">{fmtUtc(p?.data_freshness.last_updated_at)}</span>
            </li>
            <li className="flex items-center justify-between">
              <span className="text-slate-500">数据年龄</span>
              <span className="font-mono text-slate-300">
                {p?.data_freshness.data_age_seconds != null ? `${fmtNum(p.data_freshness.data_age_seconds, 0)}s` : "未知"}
              </span>
            </li>
            <li className="flex items-center justify-between">
              <span className="text-slate-500">状态</span>
              <Badge tone={p?.data_freshness.is_stale ? "warn" : "good"}>{p?.data_freshness.label ?? "未知"}</Badge>
            </li>
            <li className="flex items-center justify-between">
              <span className="text-slate-500">未同步账户</span>
              <span className="font-mono text-slate-300">{p?.data_freshness.accounts_not_synced.length ?? 0}</span>
            </li>
            <li className="pt-2 text-[10px] leading-relaxed text-slate-500">
              数据源不可用时将显示“数据过期/未知”，绝不使用旧值伪装实时数据。
            </li>
          </ul>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="最近订单" subtitle="订单状态机" dense>
          <Table
            columns={[
              { key: "time", header: "时间" },
              { key: "symbol", header: "交易对" },
              { key: "side", header: "方向" },
              { key: "status", header: "状态" },
              { key: "qty", header: "数量", align: "right" },
            ]}
            rows={(orders.data ?? []).slice(0, 8).map((o) => ({
              time: <span className="font-mono text-slate-500">{fmtUtc(o.created_at).slice(5, 19)}</span>,
              symbol: <span className="font-mono text-slate-200">{o.symbol}</span>,
              side: (
                <Badge tone={o.side === "BUY" ? "good" : "bad"}>
                  {o.side} · {o.position_side}
                </Badge>
              ),
              status: <StatusBadge status={o.status} />,
              qty: <span className="text-slate-300">{fmtNum(o.qty, 4)}</span>,
            }))}
            empty="暂无订单"
          />
        </Card>

        <Card title="系统事件与风险告警" subtitle="最近 15 条" dense>
          <Table
            columns={[
              { key: "at", header: "时间" },
              { key: "component", header: "组件" },
              { key: "type", header: "事件" },
              { key: "sev", header: "级别", align: "center" },
            ]}
            rows={(events.data ?? []).map((e) => ({
              at: <span className="font-mono text-slate-500">{fmtUtc(String(e.created_at)).slice(5, 19)}</span>,
              component: <span className="text-slate-300">{String(e.component)}</span>,
              type: <span className="font-mono text-[10px] text-slate-400">{String(e.event_type)}</span>,
              sev: <StatusBadge status={String(e.severity)} />,
            }))}
            empty="暂无系统事件"
          />
        </Card>
      </div>
    </div>
  );
}
