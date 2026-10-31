"use client";

import React, { useState } from "react";
import {
  api,
  fmtNum,
  fmtSigned,
  fmtUsd,
  fmtUtc,
  type FillItem,
  type OrderItem,
  type PositionItem,
  type StrategyItem,
  type TradeSignalItem,
} from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  DataAge,
  Empty,
  ErrorLine,
  Field,
  Modal,
  StatusBadge,
  Table,
  inputClass,
  useAction,
  usePoll,
  useToast,
} from "@/components/ui";

export function TradingSection({ view }: { view: "strategies" | "positions" | "orders" | "fills" }) {
  if (view === "strategies") return <StrategiesView />;
  if (view === "positions") return <PositionsView />;
  if (view === "orders") return <OrdersView />;
  return <FillsView />;
}

function StrategiesView() {
  const strategies = usePoll<StrategyItem[]>(api.strategies, 10000);
  const signals = usePoll<TradeSignalItem[]>(api.signals, 8000);
  const { busy, run } = useAction();
  const { push } = useToast();
  const [editing, setEditing] = useState<StrategyItem | null>(null);
  const [params, setParams] = useState("");
  const [note, setNote] = useState("参数调整");

  const openEdit = (s: StrategyItem) => {
    setEditing(s);
    setParams(JSON.stringify(s.parameters ?? {}, null, 2));
  };

  const save = async () => {
    if (!editing) return;
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(params || "{}");
    } catch {
      push("bad", "参数不是合法 JSON");
      return;
    }
    const ok = await run(
      "save",
      () => api.updateStrategy(editing.id, { parameters: parsed, change_note: note }),
      "已生成新的不可变策略版本"
    );
    if (ok) {
      setEditing(null);
      strategies.reload();
    }
  };

  return (
    <div className="space-y-4">
      {strategies.error && <ErrorLine error={strategies.error} onRetry={strategies.reload} />}

      <div className="grid gap-4 lg:grid-cols-2">
        {(strategies.data ?? []).map((s) => (
          <Card
            key={s.id}
            title={
              <div className="flex flex-wrap items-center gap-2">
                <span>{s.name}</span>
                <Badge tone="info">v{s.current_version}</Badge>
                <StatusBadge status={s.status} />
                <Badge tone="muted">{s.code}</Badge>
              </div>
            }
            subtitle={s.description}
            right={
              <div className="flex gap-1.5">
                <Button size="sm" variant="ghost" onClick={() => openEdit(s)}>
                  新版本
                </Button>
                {s.status === "ACTIVE" ? (
                  <Button size="sm" loading={busy === `pause-${s.id}`} onClick={() => run(`pause-${s.id}`, async () => { await api.pauseStrategy(s.id); strategies.reload(); }, "策略已暂停")}>
                    暂停
                  </Button>
                ) : (
                  <Button size="sm" variant="primary" loading={busy === `start-${s.id}`} onClick={() => run(`start-${s.id}`, async () => { await api.startStrategy(s.id); strategies.reload(); signals.reload(); }, "策略已启动并评估")}>
                    启动
                  </Button>
                )}
              </div>
            }
          >
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 font-mono text-[11px]">
              <dt className="text-slate-500">适用市场</dt>
              <dd className="text-right text-slate-300">{s.exchange} · {s.market_type}</dd>
              <dt className="text-slate-500">周期</dt>
              <dd className="text-right text-slate-300">{s.timeframe}</dd>
              <dt className="text-slate-500">交易对</dt>
              <dd className="text-right text-slate-300">{s.symbols.join(", ")}</dd>
              <dt className="text-slate-500">冷却时间</dt>
              <dd className="text-right text-slate-300">{s.cooldown_seconds}s</dd>
              <dt className="text-slate-500">最大持仓</dt>
              <dd className="text-right text-slate-300">{s.max_positions}</dd>
            </dl>

            <div className="mt-2 rounded border border-terminal-border/60 bg-terminal-panel/50 p-2">
              <div className="mb-1 text-[9.5px] uppercase tracking-wider text-slate-500">当前参数</div>
              <pre className="max-h-28 overflow-auto font-mono text-[10px] leading-4 text-slate-400">
                {JSON.stringify(s.parameters ?? {}, null, 2)}
              </pre>
            </div>

            {(s.versions?.length ?? 0) > 0 && (
              <div className="mt-2">
                <div className="mb-1 text-[9.5px] uppercase tracking-wider text-slate-500">
                  版本历史（不可覆盖，共 {s.versions!.length} 个）
                </div>
                <div className="space-y-0.5">
                  {s.versions!.slice(0, 4).map((v) => (
                    <div key={v.version} className="flex items-center justify-between font-mono text-[10px]">
                      <span className="text-slate-400">
                        v{v.version} · {v.change_note ?? "-"}
                      </span>
                      <span className="text-slate-600">{v.code_hash.slice(0, 10)}…</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </Card>
        ))}
        {!strategies.loading && (strategies.data?.length ?? 0) === 0 && (
          <Card><Empty>暂无策略</Empty></Card>
        )}
      </div>

      <Card title="策略信号 (Signals)" subtitle="同一信号通过 dedup_key 与冷却时间去重，不会反复触发订单" dense>
        <Table
          columns={[
            { key: "time", header: "生成时间" },
            { key: "symbol", header: "交易对" },
            { key: "dir", header: "方向" },
            { key: "tf", header: "周期" },
            { key: "score", header: "评分", align: "right" },
            { key: "status", header: "状态" },
            { key: "exp", header: "过期时间" },
            { key: "reason", header: "触发原因" },
          ]}
          rows={(signals.data ?? []).map((s) => ({
            time: <span className="font-mono text-slate-500">{fmtUtc(s.created_at).slice(5, 19)}</span>,
            symbol: <span className="font-mono text-slate-100">{s.symbol}</span>,
            dir: <Badge tone={s.direction.includes("LONG") || s.direction === "BUY" ? "good" : "bad"}>{s.direction}</Badge>,
            tf: <span className="text-slate-400">{s.timeframe}</span>,
            score: <span className="text-emerald-300">{fmtNum(s.score, 1)}</span>,
            status: <StatusBadge status={s.status} />,
            exp: <span className="font-mono text-[10px] text-slate-500">{fmtUtc(s.expires_at).slice(5, 19)}</span>,
            reason: <span className="block max-w-[420px] truncate text-[10px] text-slate-400">{s.trigger_reason}</span>,
          }))}
          empty="暂无信号"
        />
      </Card>

      <Modal open={!!editing} onClose={() => setEditing(null)} title={`新建策略版本 — ${editing?.name ?? ""}`} width="max-w-2xl">
        <div className="space-y-3">
          <p className="rounded border border-sky-500/30 bg-sky-500/10 px-2.5 py-2 text-[11px] text-sky-200">
            保存后将生成 <strong>新的不可变版本</strong>（version+1，含 SHA-256 code_hash），历史版本不会被覆盖。
          </p>
          <Field label="变更说明">
            <input className={inputClass} value={note} onChange={(e) => setNote(e.target.value)} />
          </Field>
          <Field label="参数 (JSON)">
            <textarea className={`${inputClass} h-56 resize-y`} value={params} onChange={(e) => setParams(e.target.value)} spellCheck={false} />
          </Field>
          <div className="flex justify-end gap-2">
            <Button onClick={() => setEditing(null)}>取消</Button>
            <Button variant="primary" loading={busy === "save"} onClick={save}>
              保存为新版本
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}

function PositionsView() {
  const positions = usePoll<PositionItem[]>(api.positions, 6000);
  const { busy, run } = useAction();

  return (
    <div className="space-y-4">
      {positions.error && <ErrorLine error={positions.error} onRetry={positions.reload} />}
      <Card
        title="当前持仓 (Positions)"
        subtitle="原生止盈止损（TP/SL）必须由交易所确认，本地监控仅作补充"
        right={<DataAge iso={positions.data?.[0]?.updated_at} />}
        dense
      >
        <Table
          columns={[
            { key: "exchange", header: "交易所" },
            { key: "symbol", header: "交易对" },
            { key: "side", header: "方向" },
            { key: "qty", header: "数量", align: "right" },
            { key: "entry", header: "开仓价", align: "right" },
            { key: "mark", header: "标记价", align: "right" },
            { key: "lev", header: "杠杆", align: "right" },
            { key: "margin", header: "保证金", align: "right" },
            { key: "notional", header: "名义", align: "right" },
            { key: "upnl", header: "未实现盈亏", align: "right" },
            { key: "sl", header: "止损", align: "right" },
            { key: "tp", header: "止盈", align: "right" },
            { key: "prot", header: "保护单", align: "center" },
            { key: "act", header: "", align: "center" },
          ]}
          rows={(positions.data ?? []).map((p) => ({
            exchange: <span className="font-mono uppercase text-slate-400">{p.exchange}</span>,
            symbol: <span className="font-mono text-slate-100">{p.symbol}</span>,
            side: <Badge tone={p.position_side === "LONG" || p.position_side === "SPOT" ? "good" : "bad"}>{p.position_side}</Badge>,
            qty: <span className="text-slate-300">{fmtNum(p.qty, 4)}</span>,
            entry: <span className="text-slate-300">{fmtNum(p.entry_price, 2)}</span>,
            mark: <span className="text-sky-300/90">{fmtNum(p.mark_price, 2)}</span>,
            lev: <span className="text-slate-400">{fmtNum(p.leverage, 0)}x</span>,
            margin: <span className="text-slate-400">{fmtUsd(p.margin_used)}</span>,
            notional: <span className="text-slate-400">{fmtUsd(p.notional_value)}</span>,
            upnl: <span className={Number(p.unrealized_pnl) >= 0 ? "text-emerald-300" : "text-rose-300"}>{fmtSigned(p.unrealized_pnl)}</span>,
            sl: <span className="text-rose-300/80">{p.stop_loss ? fmtNum(p.stop_loss, 2) : "—"}</span>,
            tp: <span className="text-emerald-300/80">{p.take_profit ? fmtNum(p.take_profit, 2) : "—"}</span>,
            prot: <Badge tone={p.tp_sl_confirmed ? "good" : "bad"}>{p.tp_sl_confirmed ? "CONFIRMED" : "MISSING"}</Badge>,
            act:
              p.status === "OPEN" ? (
                <Button size="sm" variant="danger" loading={busy === `close-${p.id}`} onClick={() => run(`close-${p.id}`, async () => { await api.closePosition(p.id); positions.reload(); }, "平仓请求已提交")}>
                  平仓
                </Button>
              ) : (
                <StatusBadge status={p.status} />
              ),
          }))}
          empty="暂无持仓"
        />
      </Card>
    </div>
  );
}

function OrdersView() {
  const [statusFilter, setStatusFilter] = useState<string>("");
  const orders = usePoll<OrderItem[]>(() => api.orders(statusFilter || undefined), 5000);
  const { busy, run } = useAction();
  const [reconcileTarget, setReconcileTarget] = useState<OrderItem | null>(null);
  const [remoteStatus, setRemoteStatus] = useState("FILLED");

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] text-slate-500">状态筛选</span>
        {["", "UNKNOWN", "FILLED", "PARTIALLY_FILLED", "REJECTED", "CANCELED"].map((s) => (
          <Button key={s || "all"} size="sm" variant={statusFilter === s ? "primary" : "ghost"} onClick={() => setStatusFilter(s)}>
            {s || "ALL"}
          </Button>
        ))}
        <Button size="sm" variant="ghost" className="ml-auto" onClick={() => orders.reload()}>
          刷新
        </Button>
      </div>

      {orders.error && <ErrorLine error={orders.error} onRetry={orders.reload} />}

      <Card title="订单 (Orders)" subtitle="CREATED → VALIDATED → SUBMITTED → ACKNOWLEDGED → PARTIALLY_FILLED → FILLED / REJECTED / CANCELED / EXPIRED / UNKNOWN" dense>
        <Table
          columns={[
            { key: "time", header: "时间" },
            { key: "symbol", header: "交易对" },
            { key: "cid", header: "client_order_id" },
            { key: "type", header: "类型" },
            { key: "side", header: "方向" },
            { key: "qty", header: "数量", align: "right" },
            { key: "filled", header: "已成交", align: "right" },
            { key: "price", header: "价格", align: "right" },
            { key: "avg", header: "均价", align: "right" },
            { key: "status", header: "状态" },
            { key: "prot", header: "TP/SL", align: "center" },
            { key: "act", header: "操作", align: "center" },
          ]}
          rows={(orders.data ?? []).map((o) => ({
            time: <span className="font-mono text-slate-500">{fmtUtc(o.created_at).slice(5, 19)}</span>,
            symbol: <span className="font-mono text-slate-100">{o.symbol}</span>,
            cid: <span className="font-mono text-[10px] text-slate-500">{o.client_order_id}</span>,
            type: <span className="text-slate-400">{o.order_type}</span>,
            side: <Badge tone={o.side === "BUY" ? "good" : "bad"}>{o.side}{o.reduce_only ? " · RO" : ""}</Badge>,
            qty: <span className="text-slate-300">{fmtNum(o.qty, 4)}</span>,
            filled: <span className="text-slate-400">{fmtNum(o.filled_qty, 4)}</span>,
            price: <span className="text-slate-300">{fmtNum(o.price, 2)}</span>,
            avg: <span className="text-slate-400">{Number(o.avg_fill_price) > 0 ? fmtNum(o.avg_fill_price, 2) : "—"}</span>,
            status: <StatusBadge status={o.status} />,
            prot: <Badge tone={o.tp_sl_confirmed ? "good" : "warn"}>{o.tp_sl_confirmed ? "OK" : "—"}</Badge>,
            act: (
              <div className="flex justify-center gap-1">
                {["CREATED", "VALIDATED", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED"].includes(o.status) && (
                  <Button size="sm" loading={busy === `cancel-${o.id}`} onClick={() => run(`cancel-${o.id}`, async () => { await api.cancelOrder(o.id); orders.reload(); }, "撤单已提交")}>
                    撤单
                  </Button>
                )}
                {o.status === "UNKNOWN" && (
                  <Button size="sm" variant="warn" onClick={() => setReconcileTarget(o)}>
                    对账恢复
                  </Button>
                )}
              </div>
            ),
          }))}
          empty="暂无订单"
        />
      </Card>

      <Modal open={!!reconcileTarget} onClose={() => setReconcileTarget(null)} title="UNKNOWN 订单对账恢复">
        <div className="space-y-3 text-[11px] text-slate-300">
          <p className="rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-2 text-amber-200">
            下单超时<strong>不代表交易所没有收到订单</strong>。系统将按 <code className="font-mono">client_order_id</code> 查询原订单与成交，确认后才解除阻断，绝不盲目重投。
          </p>
          <dl className="grid grid-cols-2 gap-2 font-mono text-[11px]">
            <dt className="text-slate-500">client_order_id</dt>
            <dd className="text-right text-slate-300">{reconcileTarget?.client_order_id}</dd>
            <dt className="text-slate-500">symbol</dt>
            <dd className="text-right text-slate-300">{reconcileTarget?.symbol}</dd>
            <dt className="text-slate-500">qty</dt>
            <dd className="text-right text-slate-300">{reconcileTarget?.qty}</dd>
          </dl>
          <Field label="模拟交易所返回状态">
            <select className={inputClass} value={remoteStatus} onChange={(e) => setRemoteStatus(e.target.value)}>
              <option value="FILLED">FILLED — 交易所已成交</option>
              <option value="CANCELED">CANCELED — 交易所已撤销</option>
              <option value="EXPIRED">EXPIRED — 已过期</option>
            </select>
          </Field>
          <div className="flex justify-end gap-2">
            <Button onClick={() => setReconcileTarget(null)}>取消</Button>
            <Button
              variant="primary"
              loading={busy === "reconcile"}
              onClick={async () => {
                if (!reconcileTarget) return;
                await run("reconcile", () => api.reconcileOrder(reconcileTarget.id, remoteStatus), "对账完成，订单状态已恢复");
                setReconcileTarget(null);
                orders.reload();
              }}
            >
              查询并恢复
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}

function FillsView() {
  const fills = usePoll<FillItem[]>(api.fills, 8000);
  return (
    <div className="space-y-4">
      {fills.error && <ErrorLine error={fills.error} onRetry={fills.reload} />}
      <Card title="成交明细 (Fills)" subtitle="每次真实成交独立记录，不以订单均价替代" right={<DataAge iso={fills.data?.[0]?.occurred_at} />} dense>
        <Table
          columns={[
            { key: "time", header: "时间" },
            { key: "exchange", header: "交易所" },
            { key: "symbol", header: "交易对" },
            { key: "side", header: "方向" },
            { key: "price", header: "价格", align: "right" },
            { key: "qty", header: "数量", align: "right" },
            { key: "fee", header: "手续费", align: "right" },
            { key: "pnl", header: "已实现盈亏", align: "right" },
            { key: "order", header: "订单" },
          ]}
          rows={(fills.data ?? []).map((f) => ({
            time: <span className="font-mono text-slate-500">{fmtUtc(f.occurred_at).slice(5, 19)}</span>,
            exchange: <span className="font-mono uppercase text-slate-400">{f.exchange}</span>,
            symbol: <span className="font-mono text-slate-100">{f.symbol}</span>,
            side: <Badge tone={f.side === "BUY" ? "good" : "bad"}>{f.side}</Badge>,
            price: <span className="text-slate-300">{fmtNum(f.price, 2)}</span>,
            qty: <span className="text-slate-300">{fmtNum(f.qty, 4)}</span>,
            fee: <span className="text-amber-300/90">{fmtNum(f.fee, 4)} {f.fee_currency}</span>,
            pnl: <span className={Number(f.realized_pnl) >= 0 ? "text-emerald-300" : "text-rose-300"}>{fmtSigned(f.realized_pnl)}</span>,
            order: <span className="font-mono text-[10px] text-slate-500">{f.order_id.slice(0, 8)}</span>,
          }))}
          empty="暂无成交"
        />
      </Card>
    </div>
  );
}
