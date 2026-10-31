"use client";

import React, { useState } from "react";
import {
  api,
  fmtNum,
  fmtSigned,
  fmtUsd,
  fmtUtc,
  type AnalyticsSummaryPayload,
  type BacktestItem,
  type EquityPoint,
  type JournalItem,
} from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  DataAge,
  ErrorLine,
  Field,
  Stat,
  Table,
  inputClass,
  useAction,
  usePoll,
} from "@/components/ui";
import { EquityChart } from "@/components/charts";

export function AnalyticsSection({ view }: { view: "performance" | "equity" | "backtests" | "journal" }) {
  if (view === "equity") return <EquityView />;
  if (view === "backtests") return <BacktestView />;
  if (view === "journal") return <JournalView />;
  return <PerformanceView />;
}

function PerformanceView() {
  const summary = usePoll<AnalyticsSummaryPayload>(api.analytics, 10000);

  if (summary.error) return <ErrorLine error={summary.error} onRetry={summary.reload} />;
  const s = summary.data;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4 xl:grid-cols-6">
        <Stat label="净收益" value={s ? fmtSigned(s.net_profit) : "—"} tone={Number(s?.net_profit) >= 0 ? "good" : "bad"} />
        <Stat label="胜率 Win Rate" value={s ? `${fmtNum(s.win_rate, 1)}%` : "—"} />
        <Stat label="盈亏比 Profit Factor" value={s ? fmtNum(s.profit_factor, 2) : "—"} />
        <Stat label="最大回撤 Max DD" value={s ? `${fmtNum(s.max_drawdown_pct, 2)}%` : "—"} tone="warn" />
        <Stat label="Sharpe Ratio" value={s ? fmtNum(s.sharpe_ratio, 2) : "—"} />
        <Stat label="总交易数" value={s ? String(s.total_trades) : "—"} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="策略表现 (Strategy Performance)" subtitle="按策略版本统计" dense>
          <Table
            columns={[
              { key: "k", header: "策略版本" },
              { key: "trades", header: "交易数", align: "right" },
              { key: "pnl", header: "净盈亏", align: "right" },
              { key: "fees", header: "手续费", align: "right" },
              { key: "wr", header: "胜率", align: "right" },
            ]}
            rows={Object.entries(s?.strategy_performance ?? {}).map(([k, v]) => ({
              k: <span className="font-mono text-[10px] text-slate-300">{k}</span>,
              trades: <span className="text-slate-300">{v.trades}</span>,
              pnl: <span className={Number(v.net_pnl) >= 0 ? "text-emerald-300" : "text-rose-300"}>{fmtSigned(v.net_pnl)}</span>,
              fees: <span className="text-amber-300/90">{fmtNum(v.fees, 4)}</span>,
              wr: <span className="text-slate-300">{fmtNum(v.win_rate, 1)}%</span>,
            }))}
            empty="暂无策略交易记录"
          />
        </Card>

        <Card title="交易所表现 (Exchange Performance)" subtitle="按成交统计" dense>
          <Table
            columns={[
              { key: "k", header: "交易所" },
              { key: "fills", header: "成交笔数", align: "right" },
              { key: "vol", header: "成交额", align: "right" },
              { key: "fees", header: "手续费", align: "right" },
              { key: "pnl", header: "已实现盈亏", align: "right" },
            ]}
            rows={Object.entries(s?.exchange_performance ?? {}).map(([k, v]) => ({
              k: <span className="font-mono uppercase text-slate-300">{k}</span>,
              fills: <span className="text-slate-300">{v.fills}</span>,
              vol: <span className="text-slate-400">{fmtUsd(v.volume)}</span>,
              fees: <span className="text-amber-300/90">{fmtNum(v.fees, 4)}</span>,
              pnl: <span className={Number(v.realized_pnl) >= 0 ? "text-emerald-300" : "text-rose-300"}>{fmtSigned(v.realized_pnl)}</span>,
            }))}
            empty="暂无成交记录"
          />
        </Card>
      </div>

      <Card title="统计口径说明" subtitle="Sharpe 的计算假设必须明确披露">
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="rounded border border-terminal-border/70 bg-terminal-panel/50 p-2.5">
            <div className="text-[10px] uppercase tracking-wider text-slate-500">Sharpe Ratio</div>
            <p className="mt-1 text-[10.5px] leading-relaxed text-slate-400">{s?.sharpe_note ?? "—"}</p>
          </div>
          <div className="rounded border border-terminal-border/70 bg-terminal-panel/50 p-2.5">
            <div className="text-[10px] uppercase tracking-wider text-slate-500">盈亏组成</div>
            <div className="mt-1 space-y-1 font-mono text-[10.5px]">
              <div className="flex justify-between"><span className="text-slate-500">已实现</span><span className={Number(s?.realized_pnl) >= 0 ? "text-emerald-300" : "text-rose-300"}>{fmtSigned(s?.realized_pnl)}</span></div>
              <div className="flex justify-between"><span className="text-slate-500">未实现</span><span className={Number(s?.unrealized_pnl) >= 0 ? "text-emerald-300" : "text-rose-300"}>{fmtSigned(s?.unrealized_pnl)}</span></div>
              <div className="flex justify-between"><span className="text-slate-500">手续费</span><span className="text-amber-300">-{fmtNum(s?.fees_paid, 4)}</span></div>
              <div className="flex justify-between"><span className="text-slate-500">资金费率</span><span className={Number(s?.funding_pnl) >= 0 ? "text-emerald-300" : "text-rose-300"}>{fmtSigned(s?.funding_pnl)}</span></div>
            </div>
          </div>
        </div>
      </Card>
    </div>
  );
}

function EquityView() {
  const curve = usePoll<EquityPoint[]>(api.equityCurve, 15000);
  const points = (curve.data ?? []).map((p) => ({ at: p.observed_at, value: Number(p.equity) })).filter((p) => Number.isFinite(p.value));

  return (
    <div className="space-y-4">
      {curve.error && <ErrorLine error={curve.error} onRetry={curve.reload} />}
      <Card title="净值曲线 (Equity Curve)" subtitle="来自 equity_snapshots，含已实现与未实现盈亏" right={<DataAge iso={curve.data?.[curve.data.length - 1]?.observed_at} />}>
        <EquityChart points={points} height={320} />
      </Card>
      <Card title="快照明细" dense>
        <Table
          columns={[
            { key: "t", header: "时间" },
            { key: "eq", header: "权益", align: "right" },
            { key: "rp", header: "已实现", align: "right" },
            { key: "up", header: "未实现", align: "right" },
            { key: "ne", header: "名义敞口", align: "right" },
          ]}
          rows={(curve.data ?? []).slice(-30).reverse().map((p) => ({
            t: <span className="font-mono text-slate-500">{fmtUtc(p.observed_at)}</span>,
            eq: <span className="text-slate-100">{fmtUsd(p.equity)}</span>,
            rp: <span className={Number(p.realized_pnl) >= 0 ? "text-emerald-300" : "text-rose-300"}>{fmtSigned(p.realized_pnl)}</span>,
            up: <span className={Number(p.unrealized_pnl) >= 0 ? "text-emerald-300" : "text-rose-300"}>{fmtSigned(p.unrealized_pnl)}</span>,
            ne: <span className="text-slate-400">{fmtUsd(p.notional_exposure)}</span>,
          }))}
          empty="暂无净值快照"
        />
      </Card>
    </div>
  );
}

function BacktestView() {
  const backtests = usePoll<BacktestItem[]>(api.backtests, 15000);
  const { busy, run } = useAction();
  const [form, setForm] = useState({
    strategy_code: "trend_following_v1",
    symbol: "BTC/USDT:USDT",
    exchange: "binance",
    market_type: "usdt_perp",
    timeframe: "15m",
    initial_equity: "10000",
    fee_rate: "0.0005",
    slippage_bps: "5",
    funding_rate_8h: "0.0001",
    out_of_sample_ratio: "0.3",
    bars_count: "160",
  });
  const [selected, setSelected] = useState<BacktestItem | null>(null);

  const submit = async () => {
    const result = (await run("run", () => api.runBacktest(form), "回测已完成")) as BacktestItem | null;
    if (result) {
      setSelected(result);
      backtests.reload();
    }
  };

  const metricRows = (m: Record<string, string | number> | undefined) =>
    m
      ? [
          { k: "初始权益", v: fmtUsd(m.initial_equity) },
          { k: "最终权益", v: fmtUsd(m.final_equity) },
          { k: "净盈亏", v: `${Number(m.net_pnl) >= 0 ? "+" : ""}${fmtNum(m.net_pnl, 2)}` },
          { k: "收益率", v: `${fmtNum(m.return_pct, 3)}%` },
          { k: "最大回撤", v: `${fmtNum(m.max_drawdown_pct, 3)}%` },
          { k: "交易笔数", v: String(m.trade_count) },
          { k: "胜率", v: `${fmtNum(m.win_rate, 2)}%` },
          { k: "总手续费", v: fmtNum(m.total_fees, 4) },
          { k: "总资金费率", v: fmtNum(m.total_funding, 4) },
          { k: "费用占毛利润", v: `${fmtNum(m.fee_share_of_gross, 2)}%` },
        ]
      : [];

  return (
    <div className="space-y-4">
      <Card title="回测参数" subtitle="计入手续费、滑点与资金费率；严格区分样本内 / 样本外">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="策略">
            <select className={inputClass} value={form.strategy_code} onChange={(e) => setForm({ ...form, strategy_code: e.target.value })}>
              <option value="trend_following_v1">趋势跟随 EMA+ATR</option>
              <option value="breakout_v1">区间突破 + 成交量</option>
            </select>
          </Field>
          <Field label="交易对">
            <input className={inputClass} value={form.symbol} onChange={(e) => setForm({ ...form, symbol: e.target.value })} />
          </Field>
          <Field label="周期">
            <select className={inputClass} value={form.timeframe} onChange={(e) => setForm({ ...form, timeframe: e.target.value })}>
              {["5m", "15m", "1h", "4h", "1d"].map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </Field>
          <Field label="市场类型">
            <select className={inputClass} value={form.market_type} onChange={(e) => setForm({ ...form, market_type: e.target.value })}>
              <option value="usdt_perp">USDT 永续</option>
              <option value="spot">现货</option>
            </select>
          </Field>
          <Field label="初始权益">
            <input className={inputClass} value={form.initial_equity} onChange={(e) => setForm({ ...form, initial_equity: e.target.value })} />
          </Field>
          <Field label="手续费率" hint="双边收取">
            <input className={inputClass} value={form.fee_rate} onChange={(e) => setForm({ ...form, fee_rate: e.target.value })} />
          </Field>
          <Field label="滑点 (bps)">
            <input className={inputClass} value={form.slippage_bps} onChange={(e) => setForm({ ...form, slippage_bps: e.target.value })} />
          </Field>
          <Field label="资金费率 (8h)">
            <input className={inputClass} value={form.funding_rate_8h} onChange={(e) => setForm({ ...form, funding_rate_8h: e.target.value })} />
          </Field>
          <Field label="样本外占比">
            <input className={inputClass} value={form.out_of_sample_ratio} onChange={(e) => setForm({ ...form, out_of_sample_ratio: e.target.value })} />
          </Field>
          <Field label="K 线数量">
            <input className={inputClass} value={form.bars_count} onChange={(e) => setForm({ ...form, bars_count: e.target.value })} />
          </Field>
        </div>
        <div className="mt-3 flex items-center justify-between">
          <p className="text-[10px] text-slate-500">仅使用 <code className="font-mono">is_closed=true</code> 的已收盘 K 线，避免前视偏差。</p>
          <Button variant="primary" loading={busy === "run"} onClick={submit}>
            运行回测
          </Button>
        </div>
      </Card>

      {selected && (
        <Card
          title="回测结果"
          subtitle={`${String(selected.dataset?.symbol)} · ${String(selected.dataset?.timeframe)} · ${String(selected.dataset?.source)}`}
          right={<Badge tone="warn">非未来收益保证</Badge>}
        >
          <div className="grid gap-4 lg:grid-cols-3">
            {(["in_sample", "out_of_sample", "full"] as const).map((seg) => (
              <div key={seg} className="rounded border border-terminal-border/70 bg-terminal-panel/50 p-3">
                <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-400">
                  {seg === "in_sample" ? "样本内 (In-Sample)" : seg === "out_of_sample" ? "样本外 (Out-of-Sample)" : "全区间 (Full)"}
                </div>
                <dl className="space-y-1 font-mono text-[10.5px]">
                  {metricRows(selected.metrics?.[seg]).map((r) => (
                    <div key={r.k} className="flex justify-between">
                      <dt className="text-slate-500">{r.k}</dt>
                      <dd className="text-slate-300">{r.v}</dd>
                    </div>
                  ))}
                </dl>
              </div>
            ))}
          </div>
          {selected.equity_curve && selected.equity_curve.length > 1 && (
            <div className="mt-4">
              <EquityChart points={selected.equity_curve.map((p) => ({ at: p.at, value: Number(p.equity) }))} height={200} />
            </div>
          )}
          <p className="mt-3 rounded border border-amber-500/25 bg-amber-500/[0.06] px-2.5 py-2 text-[10px] leading-relaxed text-amber-200/90">
            {selected.disclaimer}
          </p>
        </Card>
      )}

      <Card title="历史回测" dense>
        <Table
          columns={[
            { key: "t", header: "时间" },
            { key: "sym", header: "交易对" },
            { key: "tf", header: "周期" },
            { key: "ret", header: "收益率", align: "right" },
            { key: "dd", header: "最大回撤", align: "right" },
            { key: "trades", header: "交易数", align: "right" },
            { key: "oos", header: "样本外收益", align: "right" },
            { key: "act", header: "" },
          ]}
          rows={(backtests.data ?? []).map((b) => ({
            t: <span className="font-mono text-slate-500">{fmtUtc(b.created_at).slice(5, 19)}</span>,
            sym: <span className="font-mono text-slate-200">{String(b.dataset?.symbol)}</span>,
            tf: <span className="text-slate-400">{String(b.dataset?.timeframe)}</span>,
            ret: <span className={Number(b.metrics?.full?.return_pct) >= 0 ? "text-emerald-300" : "text-rose-300"}>{fmtNum(b.metrics?.full?.return_pct, 3)}%</span>,
            dd: <span className="text-amber-300/90">{fmtNum(b.metrics?.full?.max_drawdown_pct, 3)}%</span>,
            trades: <span className="text-slate-300">{String(b.metrics?.full?.trade_count ?? 0)}</span>,
            oos: <span className={Number(b.metrics?.out_of_sample?.return_pct) >= 0 ? "text-emerald-300" : "text-rose-300"}>{fmtNum(b.metrics?.out_of_sample?.return_pct, 3)}%</span>,
            act: <Button size="sm" variant="ghost" onClick={() => setSelected(b)}>查看</Button>,
          }))}
          empty="暂无回测记录"
        />
      </Card>
    </div>
  );
}

function JournalView() {
  const journal = usePoll<JournalItem[]>(api.journal, 10000);
  const [detail, setDetail] = useState<JournalItem | null>(null);

  return (
    <div className="space-y-4">
      {journal.error && <ErrorLine error={journal.error} onRetry={journal.reload} />}
      <Card
        title="交易复盘 (Journal)"
        subtitle="完整链路：信号 → AI 计划 → 风控 → 订单 → 成交 → 复盘摘要"
        right={<DataAge iso={journal.data?.[0]?.created_at} />}
        dense
      >
        <Table
          columns={[
            { key: "t", header: "时间" },
            { key: "sym", header: "交易对" },
            { key: "dir", header: "方向" },
            { key: "strat", header: "策略版本" },
            { key: "close", header: "平仓原因" },
            { key: "fees", header: "手续费", align: "right" },
            { key: "pnl", header: "净已实现盈亏", align: "right" },
            { key: "act", header: "" },
          ]}
          rows={(journal.data ?? []).map((j) => ({
            t: <span className="font-mono text-slate-500">{fmtUtc(j.created_at).slice(5, 19)}</span>,
            sym: <span className="font-mono text-slate-100">{j.symbol}</span>,
            dir: <Badge tone={j.direction.includes("LONG") || j.direction === "BUY" ? "good" : "bad"}>{j.direction}</Badge>,
            strat: <span className="font-mono text-[10px] text-slate-400">{j.strategy_version}</span>,
            close: <span className="text-slate-400">{j.close_reason}</span>,
            fees: <span className="text-amber-300/90">{fmtNum(j.fees_paid, 4)}</span>,
            pnl: <span className={Number(j.net_realized_pnl) >= 0 ? "text-emerald-300" : "text-rose-300"}>{fmtSigned(j.net_realized_pnl)}</span>,
            act: <Button size="sm" variant="ghost" onClick={() => setDetail(j)}>详情</Button>,
          }))}
          onRowClick={(row) => {
            const found = journal.data?.find((j) => j.id === String((row as unknown as { id?: string }).id));
            if (found) setDetail(found);
          }}
          empty="暂无复盘记录"
        />
      </Card>

      {detail && (
        <Card title={`复盘详情 — ${detail.symbol}`} subtitle={`${detail.exchange} · ${detail.market_type} · ${detail.strategy_version}`}>
          <div className="grid gap-3 lg:grid-cols-2">
            <div>
              <div className="mb-1 text-[10px] uppercase tracking-wider text-slate-500">决策链路</div>
              <ol className="space-y-1">
                {detail.chain.map((c, i) => (
                  <li key={i} className="flex items-center gap-2 font-mono text-[10.5px]">
                    <span className="flex h-4 w-4 items-center justify-center rounded-full bg-slate-800 text-[9px] text-slate-400">{i + 1}</span>
                    <span className="text-slate-400">{c.step}</span>
                    <span className="truncate text-slate-600">{c.ref ?? "—"}</span>
                  </li>
                ))}
              </ol>
            </div>
            <div>
              <div className="mb-1 text-[10px] uppercase tracking-wider text-slate-500">AI 输出 (TradePlan)</div>
              <pre className="max-h-52 overflow-auto rounded border border-terminal-border/60 bg-terminal-panel/60 p-2 font-mono text-[10px] leading-4 text-slate-400">
                {JSON.stringify(detail.ai_output ?? {}, null, 2)}
              </pre>
            </div>
            <div>
              <div className="mb-1 text-[10px] uppercase tracking-wider text-slate-500">风控检查结果</div>
              <pre className="max-h-52 overflow-auto rounded border border-terminal-border/60 bg-terminal-panel/60 p-2 font-mono text-[10px] leading-4 text-slate-400">
                {JSON.stringify(detail.risk_check ?? {}, null, 2)}
              </pre>
            </div>
            <div>
              <div className="mb-1 text-[10px] uppercase tracking-wider text-slate-500">复盘摘要</div>
              <p className="rounded border border-terminal-border/60 bg-terminal-panel/60 p-2 text-[11px] leading-relaxed text-slate-300">
                {detail.review_summary || "—"}
              </p>
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}
