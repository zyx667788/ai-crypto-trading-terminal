"use client";

import React, { useEffect, useMemo, useState } from "react";
import {
  api,
  fmtNum,
  fmtPct,
  fmtRate,
  fmtUtc,
  type CandleItem,
  type OpportunityItem,
  type TickerItem,
} from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  DataAge,
  Empty,
  ErrorLine,
  StatusBadge,
  Table,
  useAction,
  usePoll,
} from "@/components/ui";
import { CandleChart, OrderbookPanel } from "@/components/charts";

const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"] as const;

export function MarketsSection({ view }: { view: "overview" | "watchlist" | "scanner" }) {
  const tickers = usePoll<TickerItem[]>(api.tickers, 6000);
  const instruments = usePoll(api.instruments, 60000);
  const [selected, setSelected] = useState<TickerItem | null>(null);
  const [timeframe, setTimeframe] = useState<(typeof TIMEFRAMES)[number]>("15m");
  const [candles, setCandles] = useState<CandleItem[]>([]);
  const [book, setBook] = useState<Record<string, unknown> | null>(null);
  const [chartError, setChartError] = useState<unknown>(null);
  const [watchlist, setWatchlist] = useState<string[]>([]);

  useEffect(() => {
    if (!selected && tickers.data?.length) setSelected(tickers.data[0]);
  }, [tickers.data, selected]);

  useEffect(() => {
    if (!selected) return;
    let alive = true;
    setChartError(null);
    api
      .candles({
        exchange: selected.exchange,
        symbol: selected.symbol,
        timeframe,
        limit: 120,
        market_type: selected.market_type,
      })
      .then((c) => alive && setCandles(c))
      .catch((e) => alive && setChartError(e));
    api
      .orderbook({ exchange: selected.exchange, symbol: selected.symbol, market_type: selected.market_type })
      .then((b) => alive && setBook(b))
      .catch(() => alive && setBook(null));
    return () => {
      alive = false;
    };
  }, [selected, timeframe]);

  const lastUpdated = useMemo(
    () => tickers.data?.reduce((acc, t) => (t.received_at > acc ? t.received_at : acc), ""),
    [tickers.data]
  );
  const anyStale = tickers.data?.some((t) => t.is_stale) ?? false;

  if (view === "scanner") return <ScannerView />;

  const rows =
    view === "watchlist"
      ? (tickers.data ?? []).filter((t) => watchlist.includes(`${t.exchange}:${t.symbol}`))
      : tickers.data ?? [];

  return (
    <div className="space-y-4">
      {tickers.error && <ErrorLine error={tickers.error} onRetry={tickers.reload} />}

      <div className="grid gap-4 xl:grid-cols-3">
        <Card
          title={`${selected?.symbol ?? "-"} · ${selected?.exchange ?? ""}`}
          subtitle={`${selected?.market_type ?? ""} · Last / Mark / Index`}
          right={<DataAge iso={lastUpdated} stale={anyStale} />}
          className="xl:col-span-2"
        >
          <div className="mb-3 flex flex-wrap items-center gap-2">
            {TIMEFRAMES.map((tf) => (
              <Button key={tf} size="sm" variant={tf === timeframe ? "primary" : "ghost"} onClick={() => setTimeframe(tf)}>
                {tf}
              </Button>
            ))}
            {selected && (
              <div className="ml-auto flex flex-wrap gap-3 font-mono text-[11px]">
                <span className="text-slate-500">
                  Last <span className="text-slate-100">{fmtNum(selected.last, 2)}</span>
                </span>
                <span className="text-slate-500">
                  Mark <span className="text-sky-300">{Number(selected.mark_price) > 0 ? fmtNum(selected.mark_price, 2) : "—"}</span>
                </span>
                <span className="text-slate-500">
                  Index <span className="text-slate-400">{Number(selected.index_price) > 0 ? fmtNum(selected.index_price, 2) : "—"}</span>
                </span>
                <span className="text-slate-500">
                  Spread <span className="text-amber-300">{fmtNum(selected.spread_bps, 2)} bps</span>
                </span>
              </div>
            )}
          </div>
          {chartError ? <ErrorLine error={chartError} /> : <CandleChart candles={candles} height={310} />}
          <p className="mt-1 text-[10px] text-slate-500">
            仅展示并用于策略计算 <span className="font-mono text-slate-400">is_closed = true</span> 的已收盘 K 线，避免未收盘数据造成信号漂移。
          </p>
        </Card>

        <div className="space-y-4">
          <Card title="订单簿深度" subtitle="Bid/Ask + 不平衡度">
            <OrderbookPanel book={book} />
          </Card>
          <Card title="永续合约资金费率" subtitle="Funding Rate">
            {selected?.funding_rate ? (
              <div className="space-y-2 font-mono text-[11px]">
                <div className="flex justify-between">
                  <span className="text-slate-500">当前费率</span>
                  <span className={Number(selected.funding_rate) >= 0 ? "text-emerald-300" : "text-rose-300"}>
                    {fmtRate(selected.funding_rate, 4)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">下次结算</span>
                  <span className="text-slate-300">{fmtUtc(selected.next_funding_time)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">24h 成交额</span>
                  <span className="text-slate-300">{fmtNum(selected.quote_volume_24h, 0)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">24h 涨跌</span>
                  <span className={Number(selected.change_24h_pct) >= 0 ? "text-emerald-300" : "text-rose-300"}>
                    {fmtPct(selected.change_24h_pct)}
                  </span>
                </div>
              </div>
            ) : (
              <Empty>该交易对无资金费率（现货）</Empty>
            )}
          </Card>
        </div>
      </div>

      <Card
        title={view === "watchlist" ? "自选列表 (Watchlist)" : "市场总览 (Market Overview)"}
        subtitle={`${rows.length} 个交易对 · 数据时间 ${fmtUtc(lastUpdated)}`}
        right={<DataAge iso={lastUpdated} stale={anyStale} />}
        dense
      >
        <Table
          columns={[
            { key: "star", header: "", align: "center" },
            { key: "exchange", header: "交易所" },
            { key: "symbol", header: "交易对" },
            { key: "market", header: "市场" },
            { key: "last", header: "Last", align: "right" },
            { key: "change", header: "24h %", align: "right" },
            { key: "spread", header: "Spread bps", align: "right" },
            { key: "mark", header: "Mark", align: "right" },
            { key: "funding", header: "Funding", align: "right" },
            { key: "fresh", header: "新鲜度", align: "center" },
          ]}
          rows={rows.map((t) => ({
            star: (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  const key = `${t.exchange}:${t.symbol}`;
                  setWatchlist((w) => (w.includes(key) ? w.filter((x) => x !== key) : [...w, key]));
                }}
                className={`text-[12px] leading-none ${
                  watchlist.includes(`${t.exchange}:${t.symbol}`) ? "text-amber-300" : "text-slate-600 hover:text-slate-300"
                }`}
                aria-label="toggle watchlist"
              >
                *
              </button>
            ),
            exchange: <span className="font-mono uppercase text-slate-400">{t.exchange}</span>,
            symbol: <span className="font-mono text-slate-100">{t.symbol}</span>,
            market: <Badge tone={t.market_type === "spot" ? "info" : "neutral"}>{t.market_type === "spot" ? "SPOT" : "PERP"}</Badge>,
            last: <span className="text-slate-100">{fmtNum(t.last, 2)}</span>,
            change: <span className={Number(t.change_24h_pct) >= 0 ? "text-emerald-300" : "text-rose-300"}>{fmtPct(t.change_24h_pct)}</span>,
            spread: <span className={Number(t.spread_bps) > 15 ? "text-amber-300" : "text-slate-400"}>{fmtNum(t.spread_bps, 2)}</span>,
            mark: <span className="text-sky-300/90">{Number(t.mark_price) > 0 ? fmtNum(t.mark_price, 2) : "—"}</span>,
            funding: (
              <span className={Number(t.funding_rate) >= 0 ? "text-emerald-300/90" : "text-rose-300/90"}>
                {t.funding_rate ? fmtRate(t.funding_rate, 4) : "-"}
              </span>
            ),
            fresh: <Badge tone={t.is_stale ? "warn" : "good"}>{t.is_stale ? "STALE" : "FRESH"}</Badge>,
          }))}
          onRowClick={(row) => {
            const sym = String(row.symbol);
            const found = tickers.data?.find((t) => t.symbol === sym);
            if (found) setSelected(found);
          }}
          empty="暂无行情数据"
        />
      </Card>

      <Card title="交易规则与精度" subtitle="tick size / lot size / min notional / contract size" dense>
        <Table
          columns={[
            { key: "exchange", header: "交易所" },
            { key: "market", header: "市场" },
            { key: "native", header: "原生 Symbol" },
            { key: "canonical", header: "Canonical" },
            { key: "tick", header: "Tick", align: "right" },
            { key: "lot", header: "Lot", align: "right" },
            { key: "minQty", header: "Min Qty", align: "right" },
            { key: "minNotional", header: "Min Notional", align: "right" },
            { key: "contract", header: "Contract Size", align: "right" },
            { key: "lev", header: "Max Lev", align: "right" },
          ]}
          rows={(instruments.data ?? []).map((i) => ({
            exchange: <span className="font-mono uppercase text-slate-400">{i.exchange}</span>,
            market: <Badge tone={i.market_type === "spot" ? "info" : "neutral"}>{i.market_type}</Badge>,
            native: <span className="font-mono text-slate-300">{i.native_symbol}</span>,
            canonical: <span className="font-mono text-slate-100">{i.canonical_symbol}</span>,
            tick: <span className="text-slate-400">{i.tick_size}</span>,
            lot: <span className="text-slate-400">{i.lot_size}</span>,
            minQty: <span className="text-slate-400">{i.min_qty}</span>,
            minNotional: <span className="text-slate-400">{i.min_notional}</span>,
            contract: (
              <span className={Number(i.contract_size) !== 1 ? "text-amber-300" : "text-slate-400"}>{i.contract_size}</span>
            ),
            lev: <span className="text-slate-400">{i.max_leverage}</span>,
          }))}
          empty="暂无交易规则"
        />
      </Card>
    </div>
  );
}

function ScannerView() {
  const status = usePoll(api.scannerStatus, 6000);
  const opportunities = usePoll<OpportunityItem[]>(api.opportunities, 6000);
  const rankings = usePoll<OpportunityItem[]>(api.rankings, 6000);
  const [filter, setFilter] = useState("");
  const { busy, run } = useAction();

  const s = status.data ?? {};
  const rows = (opportunities.data ?? []).filter(
    (o) => !filter || o.symbol.toLowerCase().includes(filter.toLowerCase()) || o.exchange.includes(filter.toLowerCase())
  );

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Card title="扫描器状态">
          <div className="flex items-center justify-between">
            <StatusBadge status={String(s.state ?? s.status ?? "UNKNOWN")} />
            <span className="font-mono text-[10px] text-slate-500">{fmtUtc(String(s.last_scan_at ?? ""))}</span>
          </div>
          <div className="mt-2 text-[10px] text-slate-500">扫描间隔 {String(s.scan_interval_seconds ?? "-")}s</div>
        </Card>
        <Card title="候选机会">
          <div className="font-mono text-2xl text-slate-100">{String(s.total_scanned ?? rows.length)}</div>
          <div className="text-[10px] text-slate-500">通过风控 {String(s.passed ?? rows.filter((r) => r.risk_filter_passed).length)}</div>
        </Card>
        <Card title="进入 AI 队列">
          <div className="font-mono text-2xl text-emerald-300">
            {String(s.ai_queue_count ?? rows.filter((r) => r.enter_ai_queue).length)}
          </div>
          <div className="text-[10px] text-slate-500">仅合格候选进入分析</div>
        </Card>
        <Card title="操作">
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="primary"
              loading={busy === "start"}
              onClick={() =>
                run("start", async () => {
                  await api.startScanner();
                  status.reload();
                  opportunities.reload();
                }, "扫描已启动")
              }
            >
              启动
            </Button>
            <Button
              size="sm"
              loading={busy === "pause"}
              onClick={() =>
                run("pause", async () => {
                  await api.pauseScanner();
                  status.reload();
                }, "扫描已暂停")
              }
            >
              暂停
            </Button>
          </div>
        </Card>
      </div>

      {opportunities.error && <ErrorLine error={opportunities.error} onRetry={opportunities.reload} />}

      <Card
        title="候选机会 (Opportunities)"
        subtitle="使用批量行情与已收盘 K 线，避免逐币高频 REST 请求触及限频"
        right={
          <input
            className="w-48 rounded border border-terminal-border bg-terminal-panel px-2 py-1 text-[11px] text-slate-200 outline-none"
            placeholder="筛选 symbol / exchange"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        }
        dense
      >
        <Table
          columns={[
            { key: "score", header: "Score", align: "right" },
            { key: "symbol", header: "交易对" },
            { key: "ex", header: "交易所" },
            { key: "bias", header: "方向", align: "center" },
            { key: "tf", header: "周期" },
            { key: "rsi", header: "RSI", align: "right" },
            { key: "macd", header: "MACD Hist", align: "right" },
            { key: "atr", header: "ATR", align: "right" },
            { key: "volr", header: "Vol Ratio", align: "right" },
            { key: "spread", header: "Spread", align: "right" },
            { key: "risk", header: "风控", align: "center" },
            { key: "ai", header: "AI 队列", align: "center" },
          ]}
          rows={rows.map((o) => {
            const ind = o.indicators_snapshot ?? {};
            return {
              score: <span className="text-emerald-300">{fmtNum(o.score, 1)}</span>,
              symbol: <span className="font-mono text-slate-100">{o.symbol}</span>,
              ex: <span className="font-mono uppercase text-slate-400">{o.exchange}</span>,
              bias: <Badge tone={String(o.direction_bias).includes("LONG") ? "good" : "bad"}>{String(o.direction_bias)}</Badge>,
              tf: <span className="text-slate-400">{o.timeframe}</span>,
              rsi: <span className="text-slate-300">{fmtNum(ind.rsi_14, 1)}</span>,
              macd: <span className={Number(ind.macd_hist) >= 0 ? "text-emerald-300" : "text-rose-300"}>{fmtNum(ind.macd_hist, 3)}</span>,
              atr: <span className="text-slate-400">{fmtNum(ind.atr_14, 2)}</span>,
              volr: <span className="text-slate-300">{fmtNum(ind.volume_ratio_20, 2)}x</span>,
              spread: <span className="text-slate-400">{fmtNum(ind.spread_bps, 1)}</span>,
              risk: <Badge tone={o.risk_filter_passed ? "good" : "bad"}>{o.risk_filter_passed ? "PASS" : "REJECT"}</Badge>,
              ai: o.enter_ai_queue ? <Badge tone="info">QUEUED</Badge> : <Badge tone="muted">-</Badge>,
            };
          })}
          empty="暂无候选机会"
        />
      </Card>

      <Card title="触发原因 (Trigger Reasons)" subtitle="每个候选的可解释依据" dense>
        <div className="divide-y divide-terminal-border/40">
          {rows.slice(0, 8).map((o, i) => (
            <div key={`${o.symbol}-${i}`} className="flex flex-wrap items-start gap-2 px-3 py-2">
              <span className="w-36 shrink-0 font-mono text-[11px] text-slate-200">{o.symbol}</span>
              <div className="flex flex-1 flex-wrap gap-1">
                {(o.trigger_reasons ?? []).map((r, j) => (
                  <span key={j} className="rounded bg-slate-800/70 px-1.5 py-0.5 text-[10px] text-slate-400">
                    {r}
                  </span>
                ))}
              </div>
              {(o.trigger_reasons ?? []).length === 0 && <span className="text-[10px] text-slate-600">无触发原因</span>}
            </div>
          ))}
          {rows.length === 0 && <Empty>暂无数据</Empty>}
        </div>
      </Card>

      <Card title="综合排名 (Rankings)" subtitle="按评分排序" dense>
        <Table
          columns={[
            { key: "rank", header: "#", align: "right" },
            { key: "symbol", header: "交易对" },
            { key: "score", header: "Score", align: "right" },
            { key: "bias", header: "方向" },
            { key: "trend", header: "趋势" },
            { key: "strength", header: "强度", align: "right" },
          ]}
          rows={(rankings.data ?? []).map((o, i) => ({
            rank: <span className="text-slate-500">{i + 1}</span>,
            symbol: <span className="font-mono text-slate-100">{o.symbol}</span>,
            score: <span className="text-emerald-300">{fmtNum(o.score, 1)}</span>,
            bias: <span className="text-slate-300">{o.direction_bias}</span>,
            trend: <span className="text-slate-400">{String((o.indicators_snapshot ?? {}).trend_direction ?? "-")}</span>,
            strength: <span className="text-slate-400">{fmtNum((o.indicators_snapshot ?? {}).trend_strength, 2)}</span>,
          }))}
          empty="暂无排名"
        />
      </Card>
    </div>
  );
}
