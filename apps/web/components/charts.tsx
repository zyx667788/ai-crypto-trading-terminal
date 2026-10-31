"use client";

import React, { useMemo } from "react";
import type { CandleItem } from "@/lib/api";

/** Candlestick + volume chart rendered with pure SVG (no external chart dependency). */
export function CandleChart({
  candles,
  height = 300,
  showVolume = true,
}: {
  candles: CandleItem[];
  height?: number;
  showVolume?: boolean;
}) {
  const layout = useMemo(() => {
    if (!candles.length) return null;
    const data = candles.map((c) => ({
      o: Number(c.open),
      h: Number(c.high),
      l: Number(c.low),
      c: Number(c.close),
      v: Number(c.volume),
      t: c.open_time,
      closed: c.is_closed,
    }));
    const highs = data.map((d) => d.h);
    const lows = data.map((d) => d.l);
    const max = Math.max(...highs);
    const min = Math.min(...lows);
    const pad = (max - min) * 0.08 || max * 0.01;
    const maxV = Math.max(...data.map((d) => d.v), 1);
    return { data, max: max + pad, min: min - pad, maxV };
  }, [candles]);

  if (!layout) return <div className="py-10 text-center text-xs text-slate-500">暂无 K 线数据</div>;

  const W = 900;
  const H = height;
  const volH = showVolume ? H * 0.18 : 0;
  const priceH = H - volH - 6;
  const left = 4;
  const right = 62;
  const chartW = W - left - right;
  const step = chartW / layout.data.length;
  const bodyW = Math.max(1.5, step * 0.62);

  const yPrice = (p: number) => priceH - ((p - layout.min) / (layout.max - layout.min)) * priceH;
  const yVol = (v: number) => H - (v / layout.maxV) * volH;

  const gridLines = Array.from({ length: 5 }, (_, i) => {
    const p = layout.min + ((layout.max - layout.min) * i) / 4;
    return { p, y: yPrice(p) };
  });

  const last = layout.data[layout.data.length - 1];
  const lastUp = last.c >= last.o;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" aria-label="candlestick chart">
      {gridLines.map((g, i) => (
        <g key={i}>
          <line x1={left} x2={W - right} y1={g.y} y2={g.y} stroke="#1E293B" strokeWidth={0.7} strokeDasharray="2 4" />
          <text x={W - right + 6} y={g.y + 3.5} fill="#64748B" fontSize={9.5} fontFamily="ui-monospace, monospace">
            {g.p >= 1000 ? g.p.toFixed(0) : g.p.toFixed(2)}
          </text>
        </g>
      ))}

      {layout.data.map((d, i) => {
        const x = left + i * step + step / 2;
        const up = d.c >= d.o;
        const color = up ? "#34D399" : "#FB7185";
        const yOpen = yPrice(d.o);
        const yClose = yPrice(d.c);
        const yHigh = yPrice(d.h);
        const yLow = yPrice(d.l);
        const bodyY = Math.min(yOpen, yClose);
        const bodyHeight = Math.max(1, Math.abs(yClose - yOpen));
        return (
          <g key={`${d.t}-${i}`} opacity={d.closed ? 1 : 0.55}>
            <line x1={x} x2={x} y1={yHigh} y2={yLow} stroke={color} strokeWidth={1} />
            <rect x={x - bodyW / 2} y={bodyY} width={bodyW} height={bodyHeight} fill={up ? color : color} fillOpacity={up ? 0.9 : 0.9} />
            {showVolume && (
              <rect
                x={x - bodyW / 2}
                y={yVol(d.v)}
                width={bodyW}
                height={Math.max(0.5, H - yVol(d.v))}
                fill={color}
                fillOpacity={0.28}
              />
            )}
          </g>
        );
      })}

      <line
        x1={left}
        x2={W - right}
        y1={yPrice(last.c)}
        y2={yPrice(last.c)}
        stroke={lastUp ? "#34D399" : "#FB7185"}
        strokeWidth={0.8}
        strokeDasharray="4 3"
        opacity={0.7}
      />
      <rect x={W - right + 2} y={yPrice(last.c) - 7} width={right - 4} height={14} rx={2} fill={lastUp ? "#064E3B" : "#4C0519"} />
      <text
        x={W - right + 5}
        y={yPrice(last.c) + 3.5}
        fill={lastUp ? "#6EE7B7" : "#FDA4AF"}
        fontSize={9.5}
        fontFamily="ui-monospace, monospace"
      >
        {last.c >= 1000 ? last.c.toFixed(1) : last.c.toFixed(2)}
      </text>
    </svg>
  );
}

/** Line/area chart for equity curves. */
export function EquityChart({
  points,
  height = 220,
  label = "Equity",
}: {
  points: Array<{ at: string; value: number }>;
  height?: number;
  label?: string;
}) {
  const layout = useMemo(() => {
    if (points.length < 2) return null;
    const vals = points.map((p) => p.value);
    const max = Math.max(...vals);
    const min = Math.min(...vals);
    const pad = (max - min) * 0.15 || Math.max(1, max * 0.001);
    return { max: max + pad, min: min - pad, maxRaw: max, minRaw: min };
  }, [points]);

  if (!layout) return <div className="py-10 text-center text-xs text-slate-500">数据点不足，暂无曲线</div>;

  const W = 900;
  const H = height;
  const padL = 4;
  const padR = 64;
  const padT = 8;
  const padB = 18;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  const x = (i: number) => padL + (i / (points.length - 1)) * innerW;
  const y = (v: number) => padT + innerH - ((v - layout.min) / (layout.max - layout.min)) * innerH;

  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");
  const area = `${line} L${x(points.length - 1).toFixed(1)},${(padT + innerH).toFixed(1)} L${padL},${(padT + innerH).toFixed(1)} Z`;
  const rising = points[points.length - 1].value >= points[0].value;
  const stroke = rising ? "#34D399" : "#FB7185";

  const grid = Array.from({ length: 4 }, (_, i) => {
    const v = layout.min + ((layout.max - layout.min) * i) / 3;
    return { v, y: y(v) };
  });

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" aria-label={`${label} curve`}>
      <defs>
        <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={stroke} stopOpacity={0.28} />
          <stop offset="100%" stopColor={stroke} stopOpacity={0.01} />
        </linearGradient>
      </defs>
      {grid.map((g, i) => (
        <g key={i}>
          <line x1={padL} x2={W - padR} y1={g.y} y2={g.y} stroke="#1E293B" strokeWidth={0.7} strokeDasharray="2 4" />
          <text x={W - padR + 6} y={g.y + 3.5} fill="#64748B" fontSize={9.5} fontFamily="ui-monospace, monospace">
            {g.v.toFixed(0)}
          </text>
        </g>
      ))}
      <path d={area} fill="url(#equityFill)" />
      <path d={line} fill="none" stroke={stroke} strokeWidth={1.6} />
      <circle cx={x(points.length - 1)} cy={y(points[points.length - 1].value)} r={3} fill={stroke} />
      <text x={padL} y={H - 4} fill="#475569" fontSize={9}>
        {new Date(points[0].at).toISOString().slice(5, 16).replace("T", " ")}
      </text>
      <text x={W - padR} y={H - 4} fill="#475569" fontSize={9} textAnchor="end">
        {new Date(points[points.length - 1].at).toISOString().slice(5, 16).replace("T", " ")}
      </text>
    </svg>
  );
}

/** Horizontal depth ladder + imbalance meter. */
export function OrderbookPanel({ book }: { book: Record<string, unknown> | null }) {
  if (!book) return <div className="py-6 text-center text-xs text-slate-500">暂无盘口数据</div>;

  // Exchange adapters return either [price, qty] tuples or {price, qty} objects — normalise both.
  type Level = { price: number; qty: number };
  const normalise = (raw: unknown): Level[] => {
    if (!Array.isArray(raw)) return [];
    return raw
      .map((item): Level | null => {
        if (Array.isArray(item)) {
          const price = Number(item[0]);
          const qty = Number(item[1]);
          return Number.isFinite(price) && Number.isFinite(qty) ? { price, qty } : null;
        }
        if (item && typeof item === "object") {
          const obj = item as Record<string, unknown>;
          const price = Number(obj.price ?? obj.p);
          const qty = Number(obj.qty ?? obj.size ?? obj.q);
          return Number.isFinite(price) && Number.isFinite(qty) ? { price, qty } : null;
        }
        return null;
      })
      .filter((x): x is Level => x !== null);
  };

  const bids = normalise(book.bids);
  const asks = normalise(book.asks);
  const imbalance = Number(book.imbalance ?? book.orderbook_imbalance ?? NaN);

  if (!bids.length && !asks.length) {
    return <div className="py-6 text-center text-xs text-slate-500">盘口为空（数据源未返回深度）</div>;
  }

  const maxQty = Math.max(...bids.map((b) => b.qty), ...asks.map((a) => a.qty), 1);

  return (
    <div className="grid grid-cols-2 gap-3">
      <div>
        <div className="mb-1 flex justify-between text-[10px] uppercase tracking-wider text-slate-500">
          <span>Bids</span>
          <span>Size</span>
        </div>
        <div className="space-y-0.5">
          {bids.slice(0, 8).map((b, i) => (
            <div key={i} className="relative flex justify-between overflow-hidden rounded-sm px-1.5 py-0.5 font-mono text-[10.5px]">
              <div className="absolute inset-y-0 right-0 bg-emerald-500/10" style={{ width: `${(b.qty / maxQty) * 100}%` }} />
              <span className="relative text-emerald-300">{b.price.toFixed(2)}</span>
              <span className="relative text-slate-400">{b.qty.toFixed(4)}</span>
            </div>
          ))}
        </div>
      </div>
      <div>
        <div className="mb-1 flex justify-between text-[10px] uppercase tracking-wider text-slate-500">
          <span>Asks</span>
          <span>Size</span>
        </div>
        <div className="space-y-0.5">
          {asks.slice(0, 8).map((a, i) => (
            <div key={i} className="relative flex justify-between overflow-hidden rounded-sm px-1.5 py-0.5 font-mono text-[10.5px]">
              <div className="absolute inset-y-0 right-0 bg-rose-500/10" style={{ width: `${(a.qty / maxQty) * 100}%` }} />
              <span className="relative text-rose-300">{a.price.toFixed(2)}</span>
              <span className="relative text-slate-400">{a.qty.toFixed(4)}</span>
            </div>
          ))}
        </div>
      </div>
      {Number.isFinite(imbalance) && (
        <div className="col-span-2 mt-1">
          <div className="mb-1 flex justify-between text-[10px] text-slate-500">
            <span>Orderbook Imbalance</span>
            <span className="font-mono">{(imbalance * 100).toFixed(1)}%</span>
          </div>
          <div className="flex h-1.5 overflow-hidden rounded bg-slate-800">
            <div className="bg-emerald-500/70" style={{ width: `${Math.max(0, Math.min(100, (imbalance + 1) * 50))}%` }} />
          </div>
        </div>
      )}
    </div>
  );
}

/** Compact sparkline used in tables. */
export function Sparkline({ values, width = 90, height = 22 }: { values: number[]; width?: number; height?: number }) {
  if (values.length < 2) return <span className="text-slate-600">—</span>;
  const max = Math.max(...values);
  const min = Math.min(...values);
  const range = max - min || 1;
  const d = values
    .map((v, i) => `${i === 0 ? "M" : "L"}${(i / (values.length - 1)) * width},${height - ((v - min) / range) * height}`)
    .join(" ");
  const up = values[values.length - 1] >= values[0];
  return (
    <svg width={width} height={height} className="inline-block">
      <path d={d} fill="none" stroke={up ? "#34D399" : "#FB7185"} strokeWidth={1.2} />
    </svg>
  );
}
