"use client";

import React, { useState } from "react";
import {
  api,
  fmtAgo,
  fmtNum,
  fmtUsd,
  fmtUtc,
  type AgentRunItem,
  type AgentStatusPayload,
  type DecisionItem,
  type TradePlanPayload,
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
  useToast,
} from "@/components/ui";

const AGENT_ROLES = [
  { key: "market", label: "Market Agent", desc: "总结市场状态、波动率与资金费率环境" },
  { key: "scanner", label: "Scanner Agent", desc: "对候选机会排序与去重" },
  { key: "strategy", label: "Strategy Agent", desc: "评估规则信号并生成结构化 TradePlan" },
  { key: "risk", label: "Risk Agent", desc: "提供风险解释（不能替代确定性风控）" },
  { key: "journal", label: "Journal Agent", desc: "解释决策并生成复盘摘要" },
];

export function AISection({ view }: { view: "agents" | "decisions" | "logs" }) {
  const status = usePoll<AgentStatusPayload>(api.agentStatus, 8000);
  const runs = usePoll<AgentRunItem[]>(api.agentRuns, 8000);

  if (view === "agents") 
    return (
      <div className="space-y-4">
        {status.error && <ErrorLine error={status.error} onRetry={status.reload} />}
        <Card title="工作流状态机" subtitle="确定性状态机编排，不采用自由聊天式 Agent 群">
          <div className="grid gap-2 md:grid-cols-5">
            {AGENT_ROLES.map((role, i) => {
              const live = (status.data?.agents ?? []).find(
                (a) => (a.run_type || a.role || a.name || "").toLowerCase().includes(role.key)
              );
              return (
                <div key={role.key} className="relative rounded-md border border-terminal-border bg-terminal-panel/60 p-2.5">
                  <div className="mb-1 flex items-center justify-between">
                    <span className="text-[10px] font-semibold text-slate-300">{role.label}</span>
                    <StatusBadge status={live?.status ?? "IDLE"} />
                  </div>
                  <p className="text-[10px] leading-4 text-slate-500">{role.desc}</p>
                  <div className="mt-1.5 font-mono text-[9.5px] text-slate-600">
                    {live?.model ?? "deepseek-reasoner"} · {live?.latency_ms ?? 0}ms
                  </div>
                  {i < AGENT_ROLES.length - 1 && (
                    <span className="absolute -right-1.5 top-1/2 hidden -translate-y-1/2 text-slate-700 md:block">›</span>
                  )}
                </div>
              );
            })}
          </div>
          <p className="mt-3 text-[10px] leading-relaxed text-slate-500">
            AI 输入仅包含已脱敏的行情特征、指标、账户风险摘要与持仓摘要。<strong className="text-slate-400">AI 永不接触 API Key / Secret / Passphrase，也无法调用下单接口。</strong>
          </p>
        </Card>

        <Card title="Agent 运行统计" subtitle="延迟 / Token / 成本" dense>
          <Table
            columns={[
              { key: "agent", header: "Agent" },
              { key: "status", header: "状态" },
              { key: "model", header: "模型" },
              { key: "latency", header: "延迟", align: "right" },
              { key: "tokens", header: "Tokens", align: "right" },
              { key: "cost", header: "成本 USD", align: "right" },
              { key: "last", header: "最近运行" },
            ]}
            rows={AGENT_ROLES.map((role) => {
              const live = (status.data?.agents ?? []).find(
                (a) => (a.run_type || a.role || a.name || "").toLowerCase().includes(role.key)
              );
              return {
                agent: <span className="text-slate-200">{role.label}</span>,
                status: <StatusBadge status={live?.status ?? "IDLE"} />,
                model: <span className="font-mono text-[10px] text-slate-400">{live?.model ?? "deepseek-reasoner"}</span>,
                latency: <span className="text-slate-300">{live?.latency_ms ?? 0}</span>,
                tokens: <span className="text-slate-300">{live?.tokens ?? 0}</span>,
                cost: <span className="text-slate-400">{fmtUsd((status.data as Record<string, unknown>)?.total_cost_usd ?? 0, 5)}</span>,
                last: <span className="text-slate-500">{fmtAgo(live?.last_run_at)}</span>,
              };
            })}
            empty="暂无 Agent 记录"
          />
        </Card>
      </div>
    );

  if (view === "logs") return <AgentLogs runs={runs} />;

  return <DecisionsView />;
}

function DecisionsView() {
  const decisions = usePoll<DecisionItem[]>(api.decisions, 6000);
  const { busy, run } = useAction();
  const { push } = useToast();
  const [plan, setPlan] = useState<Record<string, unknown> | null>(null);
  const [generating, setGenerating] = useState(false);

  const generate = async (opts?: { simulate_invalid_json?: boolean; simulate_timeout?: boolean }) => {
    setGenerating(true);
    try {
      const res = await api.generateDecision(opts);
      if (res.status === "OK" && res.trade_plan) {
        setPlan(res.trade_plan);
        push("good", "AI 已生成并校验 TradePlan（仍需 Risk Engine 独立审核）");
      } else {
        setPlan(null);
        push("warn", res.message || "AI 输出未通过校验：已按 Fail-Closed 阻断新开仓");
      }
      decisions.reload();
    } catch (e) {
      push("bad", e instanceof Error ? e.message : String(e));
    } finally {
      setGenerating(false);
    }
  };

  const sendToExecution = async () => {
    if (!plan) return;
    const result = await run("submit", () => api.submitOrder(plan as unknown as TradePlanPayload), "已提交至 Risk Engine 审核");
    if (result) decisions.reload();
  };

  return (
    <div className="space-y-4">
      <Card
        title="AI 决策与 TradePlan"
        subtitle="AI 输出仅为计划，不是可直接执行的订单"
        right={
          <div className="flex flex-wrap gap-1.5">
            <Button size="sm" variant="primary" loading={generating} onClick={() => generate()}>
              生成 TradePlan
            </Button>
            <Button size="sm" variant="warn" loading={generating} onClick={() => generate({ simulate_invalid_json: true })}>
              故障注入：非法 JSON
            </Button>
            <Button size="sm" variant="warn" loading={generating} onClick={() => generate({ simulate_timeout: true })}>
              故障注入：超时
            </Button>
          </div>
        }
      >
        {plan ? (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {[
                ["交易所/市场", `${plan.exchange} · ${plan.market_type}`],
                ["交易对", String(plan.symbol)],
                ["方向", String(plan.direction)],
                ["入场类型", String(plan.entry_type)],
                ["入场价", fmtNum(plan.entry_price, 2)],
                ["止损", fmtNum(plan.stop_loss, 2)],
                ["止盈", fmtNum(plan.take_profit, 2)],
                ["置信度", fmtNum(plan.confidence, 3)],
              ].map(([k, v]) => (
                <div key={k} className="rounded border border-terminal-border/70 bg-terminal-panel/50 px-2 py-1.5">
                  <div className="text-[9.5px] uppercase tracking-wider text-slate-500">{k}</div>
                  <div className="font-mono text-[11.5px] text-slate-200">{v}</div>
                </div>
              ))}
            </div>
            <div className="rounded border border-terminal-border/70 bg-terminal-panel/50 p-2.5">
              <div className="text-[9.5px] uppercase tracking-wider text-slate-500">Thesis</div>
              <p className="mt-0.5 text-[11px] leading-relaxed text-slate-300">{String(plan.thesis)}</p>
            </div>
            <div className="rounded border border-rose-500/25 bg-rose-500/[0.06] p-2.5">
              <div className="text-[9.5px] uppercase tracking-wider text-rose-300/80">Invalidation Conditions</div>
              <ul className="mt-1 space-y-0.5 text-[11px] text-rose-200/90">
                {((plan.invalidation_conditions as string[]) ?? []).map((c, i) => (
                  <li key={i}>· {c}</li>
                ))}
              </ul>
            </div>
            <div className="flex items-center justify-between text-[10px] text-slate-500">
              <span>策略版本 {String(plan.strategy_version)} · 过期 {fmtUtc(String(plan.expires_at))}</span>
              <Button size="sm" variant="primary" loading={busy === "submit"} onClick={sendToExecution}>
                送交 Risk Engine 审核并执行
              </Button>
            </div>
          </div>
        ) : (
          <Empty>点击“生成 TradePlan”运行 5-Agent 工作流。若输出非法或超时，系统将按 Fail-Closed 阻断。</Empty>
        )}
      </Card>

      {decisions.error && <ErrorLine error={decisions.error} onRetry={decisions.reload} />}

      <Card title="AI 决策记录" subtitle="含 Schema 校验结果与风险解释" dense>
        <Table
          columns={[
            { key: "time", header: "时间" },
            { key: "symbol", header: "交易对" },
            { key: "dir", header: "方向" },
            { key: "conf", header: "置信度", align: "right" },
            { key: "valid", header: "Schema", align: "center" },
            { key: "risk", header: "Risk Agent 解释" },
          ]}
          rows={(decisions.data ?? []).map((d) => ({
            time: <span className="font-mono text-slate-500">{fmtUtc(d.created_at).slice(5, 19)}</span>,
            symbol: <span className="font-mono text-slate-100">{d.symbol}</span>,
            dir: <Badge tone={d.direction.includes("LONG") || d.direction === "BUY" ? "good" : "bad"}>{d.direction}</Badge>,
            conf: <span className="text-slate-300">{fmtNum(d.confidence, 3)}</span>,
            valid: <Badge tone={d.is_valid ? "good" : "bad"}>{d.is_valid ? "VALID" : "INVALID"}</Badge>,
            risk: <span className="block max-w-[420px] truncate text-[10px] text-slate-400">{d.risk_explanation || d.validation_error || "—"}</span>,
          }))}
          empty="暂无 AI 决策"
        />
      </Card>
    </div>
  );
}

function AgentLogs({ runs }: { runs: ReturnType<typeof usePoll<AgentRunItem[]>> }) {
  return (
    <div className="space-y-4">
      {runs.error && <ErrorLine error={runs.error} onRetry={runs.reload} />}
      <Card
        title="Agent 执行日志"
        subtitle="Prompt 版本、模型、延迟、Token 与成本（不含任何密钥）"
        right={<DataAge iso={runs.data?.[0]?.created_at} />}
        dense
      >
        <Table
          columns={[
            { key: "time", header: "时间" },
            { key: "type", header: "类型" },
            { key: "model", header: "模型" },
            { key: "status", header: "状态" },
            { key: "lat", header: "延迟 ms", align: "right" },
            { key: "pt", header: "Prompt", align: "right" },
            { key: "ct", header: "Completion", align: "right" },
            { key: "cost", header: "成本 USD", align: "right" },
            { key: "err", header: "错误" },
          ]}
          rows={(runs.data ?? []).map((r) => ({
            time: <span className="font-mono text-slate-500">{fmtUtc(r.created_at).slice(5, 19)}</span>,
            type: <span className="font-mono text-slate-300">{r.run_type}</span>,
            model: <span className="font-mono text-[10px] text-slate-400">{r.model}</span>,
            status: <StatusBadge status={r.status} />,
            lat: <span className="text-slate-300">{r.latency_ms}</span>,
            pt: <span className="text-slate-400">{r.prompt_tokens}</span>,
            ct: <span className="text-slate-400">{r.completion_tokens}</span>,
            cost: <span className="text-slate-400">{fmtUsd(r.estimated_cost_usd, 5)}</span>,
            err: <span className="block max-w-[280px] truncate text-[10px] text-rose-300/80">{r.error_message || "—"}</span>,
          }))}
          empty="暂无执行日志"
        />
      </Card>
    </div>
  );
}
