"use client";

import React, { useState } from "react";
import {
  api,
  fmtAgo,
  fmtNum,
  fmtUtc,
  type AutomationStatusPayload,
  type RecoveryStatusPayload,
  type SystemStatusPayload,
} from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorLine,
  Field,
  Modal,
  Stat,
  StatusBadge,
  Table,
  inputClass,
  useAction,
  usePoll,
  useToast,
} from "@/components/ui";
import { useWsFeed } from "@/lib/ws";

const LIVE_PHRASE = "I UNDERSTAND LIVE TRADING RISK";

export function SettingsSection({ view }: { view: "connections" | "ai" | "notifications" | "system" }) {
  if (view === "ai") return <AIProvidersView />;
  if (view === "notifications") return <NotificationsView />;
  if (view === "system") return <SystemView />;
  return <ConnectionsSettingsView />;
}

function ConnectionsSettingsView() {
  const connections = usePoll(api.connections, 10000);
  const recovery = usePoll<RecoveryStatusPayload>(api.recovery, 5000);
  const { busy, run } = useAction();

  return (
    <div className="space-y-4">
      <Card
        title="故障恢复状态机 (Recovery State Machine)"
        subtitle="HOST_BOOT → … → RECOVERY_MODE → 对账 → 保护单验证 → READY"
        right={<StatusBadge status={recovery.data?.state} />}
      >
        <div className="mb-3 flex flex-wrap gap-2">
          <Button
            size="sm"
            loading={busy === "rec"}
            onClick={async () => {
              await run("rec", api.runRecovery, "恢复流程已重新执行");
              recovery.reload();
            }}
          >
            重新执行恢复流程
          </Button>
          <Button
            size="sm"
            variant="primary"
            disabled={recovery.data?.ready}
            loading={busy === "ack"}
            onClick={async () => {
              await run("ack", () => api.acknowledgeRecovery(), "已人工确认，恢复新开仓");
              recovery.reload();
            }}
          >
            人工确认并恢复新开仓
          </Button>
        </div>
        <ol className="space-y-1">
          {(recovery.data?.steps ?? []).map((s, i) => (
            <li key={i} className="flex items-start gap-2 text-[10.5px]">
              <Badge tone={s.ok ? "good" : "bad"}>{s.ok ? "✓" : "✕"}</Badge>
              <div className="min-w-0">
                <span className="font-mono text-slate-300">{s.state}</span>
                <span className="ml-2 text-slate-500">{s.detail}</span>
              </div>
            </li>
          ))}
          {(recovery.data?.steps.length ?? 0) === 0 && <Empty>恢复流程尚未运行</Empty>}
        </ol>
        {(recovery.data?.new_entries_blocked_due_to.length ?? 0) > 0 && (
          <div className="mt-3 rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-2 text-[10px] text-amber-200">
            <strong>新开仓阻断原因：</strong>
            {recovery.data!.new_entries_blocked_due_to.map((r, i) => (
              <div key={i}>· {r}</div>
            ))}
          </div>
        )}
        <p className="mt-3 text-[10px] leading-relaxed text-slate-500">
          恢复策略：默认恢复行情与持仓监控，<strong className="text-slate-400">不自动恢复全部新开仓策略</strong>，<strong className="text-slate-400">不自动平掉全部仓位</strong>。任一对账差异、UNKNOWN 订单或行情过期都保持禁止新开仓。
        </p>
      </Card>

      <Card title="API 连接总览" dense>
        <Table
          columns={[
            { key: "ex", header: "交易所" },
            { key: "name", header: "名称" },
            { key: "env", header: "环境" },
            { key: "masked", header: "API Key (掩码)" },
            { key: "status", header: "状态" },
            { key: "sync", header: "最近同步" },
          ]}
          rows={(connections.data ?? []).map((c) => ({
            ex: <span className="font-mono uppercase text-slate-300">{c.exchange}</span>,
            name: <span className="text-slate-400">{c.name}</span>,
            env: <Badge tone={c.environment === "live" ? "bad" : "info"}>{c.environment}</Badge>,
            masked: <span className="font-mono text-slate-400">{c.api_key_masked}</span>,
            status: <StatusBadge status={c.status} />,
            sync: <span className="font-mono text-slate-500">{fmtAgo(c.last_sync_at)}</span>,
          }))}
          empty="暂无连接"
        />
      </Card>
    </div>
  );
}

function AIProvidersView() {
  const config = usePoll(api.publicConfig, 30000);
  const { busy, run } = useAction();
  const { push } = useToast();
  const c = config.data ?? {};
  return (
    <div className="space-y-4">
      <Card
        title="AI Provider 配置"
        subtitle="模型供应商解耦，可替换 Provider Adapter"
        right={
          <Button
            size="sm"
            variant="primary"
            loading={busy === "test"}
            onClick={async () => {
              const res = (await run("test", api.testAiProvider)) as Record<string, unknown> | null;
              if (res) push(res.ok ? "good" : "bad", res.ok ? `连接正常 · ${res.model} · ${res.latency_ms}ms` : `连接失败：${res.error}`);
            }}
          >
            测试连接
          </Button>
        }
      >
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Stat label="Provider" value={String(c.ai_provider ?? "—")} />
          <Stat label="Model" value={String(c.ai_model ?? "—")} />
          <Stat
            label="API Key 状态"
            value={c.ai_key_configured ? "已配置" : "未配置（规则辅助模式）"}
            tone={c.ai_key_configured ? "good" : "warn"}
          />
          <Stat label="默认环境" value={String(c.default_environment ?? "demo")} />
        </div>

        <div className="mt-3 grid gap-2 sm:grid-cols-3 font-mono text-[10.5px]">
          <div className="rounded border border-terminal-border/70 bg-terminal-panel/50 px-2.5 py-2">
            <div className="text-[9.5px] uppercase tracking-wider text-slate-500">Base URL (masked)</div>
            <div className="mt-0.5 truncate text-slate-300">{String((c.ai_provider_detail as Record<string, unknown>)?.base_url_masked ?? "—")}</div>
          </div>
          <div className="rounded border border-terminal-border/70 bg-terminal-panel/50 px-2.5 py-2">
            <div className="text-[9.5px] uppercase tracking-wider text-slate-500">Timeout / Retries</div>
            <div className="mt-0.5 text-slate-300">
              {String((c.ai_provider_detail as Record<string, unknown>)?.timeout_seconds ?? "—")}s /{" "}
              {String((c.ai_provider_detail as Record<string, unknown>)?.max_retries ?? "—")}
            </div>
          </div>
          <div className="rounded border border-terminal-border/70 bg-terminal-panel/50 px-2.5 py-2">
            <div className="text-[9.5px] uppercase tracking-wider text-slate-500">运行模式</div>
            <div className="mt-0.5 text-slate-300">
              {c.ai_key_configured ? "真实 LLM 调用 (JSON mode)" : "确定性规则模板"}
            </div>
          </div>
        </div>

        <div className="mt-4 space-y-2 rounded border border-sky-500/25 bg-sky-500/[0.06] p-3 text-[10.5px] leading-relaxed text-sky-200/90">
          <p>
            <strong>安全边界：</strong>
            AI 仅接收已脱敏的行情特征、已收盘 K 线摘要、指标值、账户风险摘要与持仓摘要。
          </p>
          <p>AI 永不可获得：API Key / Secret / Passphrase、交易所签名材料、数据库密码、任意 Shell 或代码执行权限。</p>
          <p>AI 输出仅作为 <code className="font-mono">TradePlan</code> 候选，必须通过 Pydantic Schema 校验与确定性 Risk Engine 独立审核。</p>
          <p>超时、非法输出、被截断或无响应时一律 Fail-Closed：禁止新开仓。</p>
          <p>
            <strong>过期时间是系统策略：</strong>
            模型没有时钟，因此 <code className="font-mono">expires_at</code> 由系统按周期强制执行（1m→10m，5m→15m，15m→30m，1h+→60m），
            模型给出的过去/离谱时间会被覆盖并记录在 <code className="font-mono">_expiry_policy</code> 中供审计。
          </p>
        </div>
      </Card>

      <Card title="Live 实盘闸门 (Gatekeeper)" subtitle="9 项前置检查 + 显式二次确认">
        <LiveGatekeeper />
      </Card>
    </div>
  );
}

function LiveGatekeeper() {
  const automation = usePoll<AutomationStatusPayload>(api.automationStatus, 8000);
  const { busy, run } = useAction();
  const { push } = useToast();
  const [open, setOpen] = useState(false);
  const [phrase, setPhrase] = useState("");
  const checklist = automation.data?.live_preflight;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-slate-400">当前 Live 状态</span>
          <Badge tone={automation.data?.live_trading_enabled ? "bad" : "good"}>
            {automation.data?.live_trading_enabled ? "ENABLED" : "DISABLED (默认关闭)"}
          </Badge>
        </div>
        <Badge tone={checklist?.ready ? "good" : "warn"}>{checklist?.ready ? "可进入实盘" : "未通过前置检查"}</Badge>
      </div>

      <ul className="space-y-1">
        {(checklist?.items ?? []).map((item, i) => (
          <li key={i} className="flex items-start gap-2 text-[10.5px]">
            <Badge tone={item.passed ? "good" : "bad"}>{item.passed ? "PASS" : "BLOCK"}</Badge>
            <span className="text-slate-400">{item.item}</span>
          </li>
        ))}
      </ul>

      <p className="rounded border border-amber-500/25 bg-amber-500/[0.06] px-2.5 py-2 text-[10px] leading-relaxed text-amber-200/90">
        {checklist?.note}
      </p>

      <Button variant="danger" onClick={() => setOpen(true)}>
        启动 Live 实盘（需二次确认）
      </Button>

      <Modal open={open} onClose={() => setOpen(false)} title="⚠ 启动 Live 实盘">
        <div className="space-y-3 text-[11px] text-slate-300">
          <p className="rounded border border-rose-500/40 bg-rose-500/10 px-2.5 py-2 text-rose-200">
            实盘可能发生<strong>超过预设风险预算的损失</strong>。系统不承诺收益，也不保证损失上限。
          </p>
          <p>请完整输入以下确认短语：</p>
          <code className="block rounded bg-slate-800 px-2 py-1 font-mono text-[11px] text-amber-300">{LIVE_PHRASE}</code>
          <Field label="确认短语">
            <input className={inputClass} value={phrase} onChange={(e) => setPhrase(e.target.value)} placeholder={LIVE_PHRASE} />
          </Field>
          <div className="flex justify-end gap-2">
            <Button onClick={() => setOpen(false)}>取消</Button>
            <Button
              variant="danger"
              disabled={phrase !== LIVE_PHRASE}
              loading={busy === "live"}
              onClick={async () => {
                const res = await run("live", () => api.startAutomation("live"));
                if (!res) push("warn", "Live 启动被拒绝：请先在后端显式设置 LIVE_TRADING_ENABLED=true 并完成全部前置检查。");
                setOpen(false);
                automation.reload();
              }}
            >
              确认启动实盘
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}

function NotificationsView() {
  const system = usePoll<SystemStatusPayload>(api.systemStatus, 10000);
  const notif = usePoll(() => api.publicConfig(), 30000);

  return (
    <div className="space-y-4">
      {system.error && <ErrorLine error={system.error} onRetry={system.reload} />}
      <Card title="告警渠道 (Alert Channels)" subtitle="Mac 整机离线只能由外部心跳检测发现">
        <div className="grid gap-2 sm:grid-cols-2">
          {Object.entries(system.data?.notification_channels ?? {}).map(([k, v]) => (
            <div key={k} className="flex items-center justify-between rounded border border-terminal-border/70 bg-terminal-panel/50 px-2.5 py-2">
              <span className="font-mono text-[11px] text-slate-300">{k}</span>
              <Badge tone={v ? "good" : "muted"}>{v ? "CONFIGURED" : "NOT SET"}</Badge>
            </div>
          ))}
          {(Object.keys(system.data?.notification_channels ?? {}).length ?? 0) === 0 && <Empty>暂无渠道</Empty>}
        </div>

        <div className="mt-3 rounded border border-terminal-border/70 bg-terminal-panel/50 p-2.5">
          <div className="flex items-center justify-between">
            <span className="text-[11px] text-slate-300">外部心跳 (External Heartbeat)</span>
            <Badge tone={system.data?.external_heartbeat.configured ? "good" : "warn"}>
              {system.data?.external_heartbeat.configured ? "CONFIGURED" : "NOT CONFIGURED"}
            </Badge>
          </div>
          <p className="mt-1 text-[10px] leading-relaxed text-slate-500">
            Mac 断电或网络中断时，本机日志无法自我上报。需配置外部心跳服务（如 Healthchecks.io）在整机离线时通知你。当前已配置：{String(notif.data?.ai_key_configured ? "—" : "否")}
          </p>
        </div>
      </Card>

      <Card title="告警阈值" dense>
        <Table
          columns={[
            { key: "k", header: "监控项" },
            { key: "v", header: "阈值" },
          ]}
          rows={[
            { k: "磁盘可用空间", v: "< 20% 告警" },
            { k: "内存压力", v: "≥ 80% 黄色 / ≥ 92% 红色" },
            { k: "行情数据新鲜度", v: "超过策略允许阈值即标记过期并禁止新开仓" },
            { k: "交易所私有 WebSocket", v: "断开 → REST 对账 + 暂停新开仓" },
            { k: "订单 UNKNOWN", v: "超过处理时限 → 告警" },
            { k: "本地与交易所持仓", v: "不一致 → 冻结账户新开仓" },
            { k: "日亏损", v: "达到风控阈值 → 锁定新开仓" },
            { k: "备份", v: "失败或超时 → 告警" },
          ]}
        />
      </Card>
    </div>
  );
}

function SystemView() {
  const system = usePoll<SystemStatusPayload>(api.systemStatus, 6000);
  const events = usePoll(() => api.systemEvents(30), 8000);
  const rateLimits = usePoll(() => api.rateLimits(), 5000);
  const { busy, run } = useAction();
  const { push } = useToast();
  const r = system.data?.resource;

  // Push feed: speeds up refresh, but polling above remains the source of truth.
  const feed = useWsFeed("system", { onEvent: () => events.reload() });
  const rateBuckets = (rateLimits.data?.buckets ?? []) as Array<Record<string, unknown>>;

  return (
    <div className="space-y-4">
      {system.error && <ErrorLine error={system.error} onRetry={system.reload} />}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="进程存活" value={system.data?.liveness.status ?? "—"} tone="good" sub={`PID ${system.data?.liveness.pid ?? "—"}`} />
        <Stat
          label="依赖就绪"
          value={system.data?.readiness.status ?? "—"}
          tone={system.data?.readiness.status === "ready" ? "good" : "bad"}
          sub={system.data?.readiness.trading_allowed ? "允许交易" : "禁止新开仓"}
        />
        <Stat
          label="内存压力"
          value={r ? `${r.memory.pressure_level} · ${fmtNum(r.memory.usage_pct, 1)}%` : "—"}
          tone={r?.memory.pressure_level === "GREEN" ? "good" : r?.memory.pressure_level === "YELLOW" ? "warn" : "bad"}
          sub={r ? `${r.memory.available_mb} MB 可用 / ${r.memory.total_mb} MB` : ""}
        />
        <Stat
          label="磁盘可用"
          value={r ? `${fmtNum(r.disk.free_pct, 1)}%` : "—"}
          tone={r && r.disk.free_pct >= 20 ? "good" : "warn"}
          sub={r ? `${r.disk.free_gb} GB free` : ""}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="16GB 统一内存预算 (Mac mini)" subtitle="目标预算，需用 Activity Monitor / memory_pressure 校准">
          <Table
            columns={[
              { key: "k", header: "进程 / 用途" },
              { key: "v", header: "预算 MB", align: "right" },
            ]}
            rows={Object.entries(r?.memory.unified_16gb_budget_mb ?? {}).map(([k, v]) => ({
              k: <span className="text-slate-300">{k.replace(/_/g, " ")}</span>,
              v: <span className="text-slate-400">{v}</span>,
            }))}
            empty="—"
          />
          <p className="mt-2 text-[10px] leading-relaxed text-slate-500">{r?.memory.degradation_guidance}</p>
        </Card>

        <div className="space-y-4">
          <Card title="Watchdog 健康检查" subtitle="每 30–60 秒检查一次">
            <div className="grid grid-cols-2 gap-2">
              {Object.entries(system.data?.readiness.checks ?? {}).map(([k, v]) => (
                <div key={k} className="flex items-center justify-between rounded border border-terminal-border/70 bg-terminal-panel/50 px-2 py-1.5">
                  <span className="font-mono text-[10px] text-slate-400">{k}</span>
                  <Badge tone={v ? "good" : "bad"}>{v ? "OK" : "FAIL"}</Badge>
                </div>
              ))}
            </div>
            <div className="mt-2 grid grid-cols-2 gap-2 text-[10px] text-slate-500">
              <div>主机运行 {Math.round((system.data?.liveness.host_uptime_seconds ?? 0) / 3600)}h</div>
              <div>进程运行 {Math.round((system.data?.liveness.process_uptime_seconds ?? 0) / 60)}m</div>
              <div>CPU {fmtNum(r?.cpu_pct, 1)}%</div>
              <div>Swap {r?.memory.swap_used_mb ?? 0} MB</div>
            </div>
          </Card>

          <Card title="备份与恢复演练" subtitle="每日加密备份，保留 7 日 + 4 周">
            <div className="mb-2 flex items-center justify-between text-[11px]">
              <span className="text-slate-500">备份数量</span>
              <span className="font-mono text-slate-300">{system.data?.backups.count ?? 0}</span>
            </div>
            <div className="mb-2 flex items-center justify-between text-[11px]">
              <span className="text-slate-500">最近备份</span>
              <span className="font-mono text-slate-300">
                {system.data?.backups.latest_age_hours != null ? `${fmtNum(system.data.backups.latest_age_hours, 1)}h ago` : "—"}
              </span>
            </div>
            {system.data?.backups.stale && <Badge tone="warn">备份不足或已过期</Badge>}
            <div className="mt-3 flex gap-2">
              <Button
                size="sm"
                loading={busy === "backup"}
                onClick={async () => {
                  await run("backup", () => api.createBackup("manual"), "加密备份已创建");
                  system.reload();
                }}
              >
                立即备份
              </Button>
              <Button
                size="sm"
                loading={busy === "restore"}
                onClick={async () => {
                  const res = (await run("restore", api.restoreCheck)) as Record<string, unknown> | null;
                  if (res) push(res.ok ? "good" : "bad", res.ok ? `恢复演练通过：${res.restored_rows} 行` : `恢复演练失败：${res.reason ?? res.error}`);
                }}
              >
                恢复演练
              </Button>
            </div>
            <p className="mt-2 text-[10px] text-slate-500">
              备份内容已脱敏且不含明文 API Secret；恢复验证在隔离读取器中完成，不影响生产库。
            </p>
          </Card>
        </div>
      </div>

      <Card title="launchd 常驻服务" subtitle="生产常驻：原生 macOS 进程 + launchd + Homebrew；Docker 仅用于开发/测试">
        <Table
          columns={[
            { key: "svc", header: "服务" },
            { key: "role", header: "职责" },
            { key: "budget", header: "内存预算 MB", align: "right" },
            { key: "status", header: "状态", align: "center" },
            { key: "last", header: "最近运行" },
          ]}
          rows={[
            { svc: "com.aicrypto.api", role: "FastAPI API", budget: "1000", status: "见 /health", last: "—" },
            { svc: "com.aicrypto.web", role: "Next.js Web", budget: "800", status: "见前端", last: "—" },
            { svc: "com.aicrypto.market-worker", role: "Market / Strategy Worker", budget: "1500", status: "launchd", last: "—" },
            { svc: "com.aicrypto.execution-worker", role: "Execution Worker", budget: "1000", status: "launchd", last: "—" },
            { svc: "com.aicrypto.recovery-watchdog", role: "Recovery Watchdog", budget: "300", status: "launchd", last: "—" },
            { svc: "com.aicrypto.postgres", role: "PostgreSQL (native)", budget: "1500", status: "brew", last: "—" },
            { svc: "com.aicrypto.redis", role: "Redis (native)", budget: "300", status: "brew", last: "—" },
          ]}
        />
        <p className="mt-2 text-[10px] leading-relaxed text-slate-500">
          注：启用 FileVault 时 macOS 自动登录不可用，<code className="font-mono">~/Library/LaunchAgents</code> 需在用户登录会话建立后才会运行。本产品<strong className="text-slate-400">不声称</strong>已实现 100% 无人值守冷启动高可用。
        </p>
      </Card>

      <Card
        title="实时事件流 (WebSocket)"
        subtitle="8 个通道 · 断线自动重连（指数退避 + 抖动，上限 30s）"
        right={
          <div className="flex items-center gap-2">
            <Badge tone={feed.state === "OPEN" ? "good" : feed.state === "RECONNECTING" ? "warn" : "muted"}>
              {feed.state}
            </Badge>
            <span className="font-mono text-[10px] text-slate-500">{feed.events.length} events</span>
            <Button size="sm" variant="ghost" onClick={feed.reconnect}>
              重连
            </Button>
          </div>
        }
        dense
      >
        {feed.events.length === 0 ? (
          <Empty>
            已连接 `/ws/system`，等待事件。流水线运行、风控触发、备份等操作都会实时推送。
          </Empty>
        ) : (
          <Table
            columns={[
              { key: "at", header: "时间" },
              { key: "type", header: "事件类型" },
              { key: "src", header: "来源" },
              { key: "payload", header: "数据" },
            ]}
            rows={feed.events.slice(0, 12).map((e) => ({
              at: <span className="font-mono text-slate-500">{fmtUtc(e.occurred_at).slice(5, 19)}</span>,
              type: <span className="font-mono text-[10px] text-slate-200">{e.event_type}</span>,
              src: <span className="text-slate-400">{e.source}</span>,
              payload: (
                <span className="block max-w-[520px] truncate text-[10px] text-slate-500">
                  {JSON.stringify(e.data).slice(0, 200)}
                </span>
              ),
            }))}
          />
        )}
      </Card>

      <Card title="交易所限频预算 (Rate Limits)" subtitle="加权令牌桶 · 每 (交易所, 市场) 独立配额" dense>
        <Table
          columns={[
            { key: "bucket", header: "Bucket" },
            { key: "policy", header: "策略" },
            { key: "available", header: "可用", align: "right" },
            { key: "capacity", header: "容量", align: "right" },
            { key: "util", header: "使用率", align: "right" },
            { key: "rejected", header: "拒绝", align: "right" },
            { key: "waited", header: "累计等待", align: "right" },
          ]}
          rows={rateBuckets.map((b) => ({
            bucket: <span className="font-mono text-slate-200">{String(b.bucket)}</span>,
            policy: <span className="text-[10px] text-slate-500">{String(b.policy)}</span>,
            available: <span className="text-slate-300">{fmtNum(b.available, 1)}</span>,
            capacity: <span className="text-slate-500">{fmtNum(b.capacity, 0)}</span>,
            util: (
              <span className={Number(b.utilization_pct) > 70 ? "text-amber-300" : "text-slate-400"}>
                {fmtNum(b.utilization_pct, 1)}%
              </span>
            ),
            rejected: (
              <span className={Number(b.rejected_total) > 0 ? "text-rose-300" : "text-slate-500"}>
                {String(b.rejected_total)}
              </span>
            ),
            waited: <span className="text-slate-500">{fmtNum(b.waited_seconds, 2)}s</span>,
          }))}
          empty="暂无配额数据"
        />
      </Card>

      <Card title="WebSocket 事件流 / 系统事件" subtitle="8 个统一通道：dashboard, market, scanner, agents, orders, positions, risk, system" dense>
        <Table
          columns={[
            { key: "t", header: "时间" },
            { key: "c", header: "组件" },
            { key: "e", header: "事件" },
            { key: "s", header: "级别", align: "center" },
          ]}
          rows={(events.data ?? []).map((e) => ({
            t: <span className="font-mono text-slate-500">{fmtUtc(String(e.created_at)).slice(5, 19)}</span>,
            c: <span className="text-slate-300">{String(e.component)}</span>,
            e: <span className="font-mono text-[10px] text-slate-400">{String(e.event_type)}</span>,
            s: <StatusBadge status={String(e.severity)} />,
          }))}
          empty="暂无系统事件"
        />
      </Card>
    </div>
  );
}
