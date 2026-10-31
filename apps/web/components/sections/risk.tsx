"use client";

import React, { useEffect, useState } from "react";
import { api, fmtUsd, fmtUtc, type RiskEventItem, type RiskLimits, type RiskStatusPayload } from "@/lib/api";
import {
  Button,
  Card,
  DataAge,
  ErrorLine,
  Field,
  Modal,
  StatusBadge,
  Table,
  inputClass,
  useAction,
  usePoll,
} from "@/components/ui";

const LIMIT_FIELDS: Array<{ key: keyof RiskLimits; label: string; hint: string; kind: "decimal" | "int" }> = [
  { key: "max_risk_per_trade_pct", label: "每笔最大风险", hint: "占权益比例，如 0.0025 = 0.25%", kind: "decimal" },
  { key: "max_daily_loss_pct", label: "每日最大亏损", hint: "0.01 = 1%", kind: "decimal" },
  { key: "max_total_exposure_pct", label: "最大总敞口", hint: "0.30 = 30%", kind: "decimal" },
  { key: "max_symbol_exposure_pct", label: "单交易对最大敞口", hint: "0.15 = 15%", kind: "decimal" },
  { key: "max_open_positions", label: "最大并行持仓数", hint: "整数", kind: "int" },
  { key: "max_leverage", label: "最大杠杆", hint: "如 2 = 2x", kind: "decimal" },
  { key: "max_slippage_bps", label: "最大滑点 (bps)", hint: "25 bps", kind: "decimal" },
  { key: "max_spread_bps", label: "最大买卖价差 (bps)", hint: "20 bps", kind: "decimal" },
  { key: "max_data_stale_seconds", label: "最大行情延迟 (秒)", hint: "30 秒", kind: "int" },
  { key: "max_consecutive_api_failures", label: "连续 API 失败上限", hint: "次", kind: "int" },
  { key: "max_consecutive_losses", label: "连续亏损上限", hint: "次", kind: "int" },
  { key: "max_daily_trades", label: "当日交易次数上限", hint: "次", kind: "int" },
];

export function RiskSection({ view }: { view: "profile" | "events" | "killswitch" }) {
  const profile = usePoll(api.riskProfile, 20000);
  const status = usePoll<RiskStatusPayload>(api.riskStatus, 6000);
  const events = usePoll<RiskEventItem[]>(api.riskEvents, 8000);
  const { busy, run } = useAction();

  const [form, setForm] = useState<Record<string, string>>({});
  useEffect(() => {
    if (profile.data?.limits) {
      const next: Record<string, string> = {};
      Object.entries(profile.data.limits).forEach(([k, v]) => {
        if (typeof v !== "boolean") next[k] = String(v);
      });
      setForm(next);
    }
  }, [profile.data]);

  const killOn = Boolean(status.data?.kill_switch?.enabled ?? status.data?.kill_switch_enabled);

  if (view === "events") {
    return (
      <div className="space-y-4">
        {events.error && <ErrorLine error={events.error} onRetry={events.reload} />}
        <Card title="风险事件 (Risk Events)" subtitle="每次风控拦截与告警均可审计" right={<DataAge iso={events.data?.[0]?.created_at} />} dense>
          <Table
            columns={[
              { key: "time", header: "时间" },
              { key: "rule", header: "规则" },
              { key: "type", header: "事件类型" },
              { key: "sev", header: "级别", align: "center" },
              { key: "detail", header: "详情" },
            ]}
            rows={(events.data ?? []).map((e) => ({
              time: <span className="font-mono text-slate-500">{fmtUtc(e.created_at).slice(5, 19)}</span>,
              rule: <span className="font-mono text-[10px] text-slate-300">{e.rule_code}</span>,
              type: <span className="font-mono text-[10px] text-slate-400">{e.event_type}</span>,
              sev: <StatusBadge status={e.severity} />,
              detail: (
                <span className="block max-w-[560px] truncate text-[10px] text-slate-400">
                  {String(e.details?.reason ?? e.details?.mismatches ?? JSON.stringify(e.details)).slice(0, 220)}
                </span>
              ),
            }))}
            empty="暂无风险事件"
          />
        </Card>
      </div>
    );
  }

  if (view === "killswitch") return <KillSwitchView killOn={killOn} status={status} />;

  return (
    <div className="space-y-4">
      {profile.error && <ErrorLine error={profile.error} onRetry={profile.reload} />}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Card title="风控状态">
          <div className="flex items-center gap-2">
            <StatusBadge status={killOn ? "LOCKED" : "ACTIVE"} />
            <span className="text-[10px] text-slate-500">{killOn ? "Kill Switch 已触发" : "允许新开仓（需通过全部规则）"}</span>
          </div>
        </Card>
        <Card title="当日已实现亏损">
          <div className="font-mono text-lg text-rose-300">{fmtUsd(status.data?.daily_realized_loss ?? 0)}</div>
          <div className="text-[10px] text-slate-500">当日交易 {String(status.data?.daily_trades_count ?? 0)} 笔</div>
        </Card>
        <Card title="连续亏损">
          <div className="font-mono text-lg text-amber-300">{String(status.data?.consecutive_losses ?? 0)}</div>
          <div className="text-[10px] text-slate-500">达到上限自动锁定</div>
        </Card>
        <Card title="当前持仓">
          <div className="font-mono text-lg text-slate-100">{String(status.data?.open_positions_count ?? 0)}</div>
          <div className="text-[10px] text-slate-500">敞口 {fmtUsd(status.data?.total_exposure ?? 0)}</div>
        </Card>
      </div>

      <Card
        title="风控参数 (Risk Profile)"
        subtitle="修改后立即写入审计日志；AI 无权修改任何风控上限"
        right={
          <Button
            variant="primary"
            size="sm"
            loading={busy === "save"}
            onClick={async () => {
              const payload: Record<string, unknown> = {};
              LIMIT_FIELDS.forEach((f) => {
                payload[f.key] = f.kind === "int" ? parseInt(form[f.key] ?? "0", 10) : form[f.key];
              });
              const ok = await run("save", () => api.updateRiskProfile(payload as Partial<RiskLimits>), "风控参数已更新");
              if (ok) profile.reload();
            }}
          >
            保存风控参数
          </Button>
        }
      >
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {LIMIT_FIELDS.map((f) => (
            <Field key={f.key} label={f.label} hint={f.hint}>
              <input
                className={inputClass}
                value={form[f.key] ?? ""}
                onChange={(e) => setForm({ ...form, [f.key]: e.target.value })}
                inputMode={f.kind === "int" ? "numeric" : "decimal"}
              />
            </Field>
          ))}
        </div>
        <p className="mt-3 rounded border border-amber-500/25 bg-amber-500/[0.06] px-2.5 py-2 text-[10px] leading-relaxed text-amber-200/90">
          风控引擎自身异常时采用 <strong>Fail Closed</strong>：拒绝新开仓。已有仓位的保护与对账不依赖 AI 服务持续运行。
        </p>
      </Card>
    </div>
  );
}

function KillSwitchView({ killOn, status }: { killOn: boolean; status: ReturnType<typeof usePoll<RiskStatusPayload>> }) {
  const { busy, run } = useAction();
  const [killReason, setKillReason] = useState("Manual operator emergency stop");
  const [flattenOpen, setFlattenOpen] = useState(false);
  const [resumeCode, setResumeCode] = useState("");
  const [resumeOpen, setResumeOpen] = useState(false);
  const [resetCode, setResetCode] = useState("");
  const [resetOpen, setResetOpen] = useState(false);

  return (
    <div className="space-y-4">
      <Card
        title="紧急熔断 (Kill Switch)"
        subtitle="禁止一切新开仓；不会自动平掉已有仓位"
        right={<StatusBadge status={killOn ? "LOCKED" : "ACTIVE"} />}
      >
        <div className="grid gap-4 lg:grid-cols-2">
          <div className="space-y-2">
            <Field label="触发原因">
              <input className={inputClass} value={killReason} onChange={(e) => setKillReason(e.target.value)} />
            </Field>
            <Button
              variant="danger"
              loading={busy === "kill"}
              onClick={async () => {
                await run("kill", () => api.killSwitch(killReason), "Kill Switch 已触发，新开仓已禁止");
                status.reload();
              }}
            >
              触发 Kill Switch（禁止新开仓）
            </Button>
          </div>

          <div className="space-y-2">
            <p className="text-[10px] leading-relaxed text-slate-500">
              恢复交易需输入确认码 <code className="font-mono text-amber-300">CONFIRM_RESUME</code>，并在确认已完成后对账、风险事件已复盘的前提下执行。
            </p>
            <Button variant="primary" onClick={() => setResumeOpen(true)}>
              人工确认恢复交易
            </Button>
          </div>
        </div>

        <hr className="my-4 border-terminal-border" />

        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-[12px] font-semibold text-slate-200">复位日亏损 / 连续亏损锁定</div>
            <p className="mt-0.5 text-[10px] text-slate-500">
              达到日亏损或连续亏损上限后，新开仓会被锁定。复位会记录一个时间水位线，
              <strong className="text-slate-400">不会删除任何订单、成交或复盘记录</strong>。确认码 <code className="font-mono text-amber-300">CONFIRM_RESET</code>。
            </p>
          </div>
          <Button variant="warn" onClick={() => setResetOpen(true)}>
            复位日亏损锁定
          </Button>
        </div>

        <hr className="my-4 border-terminal-border" />

        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-[12px] font-semibold text-slate-200">紧急全部平仓 (Flatten All)</div>
            <p className="mt-0.5 text-[10px] text-slate-500">
              使用 reduce-only 平仓语义。该操作不可撤销，需二次确认。
            </p>
          </div>
          <Button variant="danger" onClick={() => setFlattenOpen(true)}>
            紧急平仓
          </Button>
        </div>
      </Card>

      <Modal open={resetOpen} onClose={() => setResetOpen(false)} title="复位日亏损 / 连续亏损锁定">
        <div className="space-y-3">
          <p className="rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-2 text-[11px] text-amber-200">
            复位后，风控的日亏损 / 连续亏损计数器只统计此刻之后的活动。历史成交与审计记录全部保留。
            请确认你已复盘亏损原因，并理解继续交易可能带来更多损失。
          </p>
          <Field label="确认码">
            <input
              className={inputClass}
              value={resetCode}
              onChange={(e) => setResetCode(e.target.value)}
              placeholder="CONFIRM_RESET"
            />
          </Field>
          <div className="flex justify-end gap-2">
            <Button onClick={() => setResetOpen(false)}>取消</Button>
            <Button
              variant="warn"
              disabled={resetCode !== "CONFIRM_RESET"}
              loading={busy === "resetLocks"}
              onClick={async () => {
                const ok = await run("resetLocks", () => api.resetDailyLocks(resetCode), "日亏损锁定已复位（历史记录已保留）");
                if (ok) {
                  setResetOpen(false);
                  setResetCode("");
                  status.reload();
                }
              }}
            >
              确认复位
            </Button>
          </div>
        </div>
      </Modal>

      <Modal open={flattenOpen} onClose={() => setFlattenOpen(false)} title="⚠ 确认紧急平仓">
        <div className="space-y-3 text-[11px] text-slate-300">
          <p className="rounded border border-rose-500/40 bg-rose-500/10 px-2.5 py-2 text-rose-200">
            此操作将撤销全部活跃订单并以 <code className="font-mono">reduce_only=True</code> 平掉所有未平仓持仓。该操作<strong>不可撤销</strong>。
          </p>
          <div className="flex justify-end gap-2">
            <Button onClick={() => setFlattenOpen(false)}>取消</Button>
            <Button
              variant="danger"
              loading={busy === "flatten"}
              onClick={async () => {
                await run("flatten", () => api.flatten(), "紧急平仓已提交");
                setFlattenOpen(false);
                status.reload();
              }}
            >
              确认全部平仓
            </Button>
          </div>
        </div>
      </Modal>

      <Modal open={resumeOpen} onClose={() => setResumeOpen(false)} title="确认恢复交易">
        <div className="space-y-3">
          <p className="text-[11px] text-slate-400">
            请输入确认码 <code className="font-mono text-amber-300">CONFIRM_RESUME</code>。
          </p>
          <Field label="确认码">
            <input className={inputClass} value={resumeCode} onChange={(e) => setResumeCode(e.target.value)} placeholder="CONFIRM_RESUME" />
          </Field>
          <div className="flex justify-end gap-2">
            <Button onClick={() => setResumeOpen(false)}>取消</Button>
            <Button
              variant="primary"
              disabled={resumeCode !== "CONFIRM_RESUME"}
              loading={busy === "resume"}
              onClick={async () => {
                const ok = await run("resume", () => api.resume(resumeCode), "已人工确认恢复");
                if (ok) {
                  setResumeOpen(false);
                  setResumeCode("");
                  status.reload();
                }
              }}
            >
              确认恢复
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
