"use client";

import React, { useState } from "react";
import {
  api,
  fmtAgo,
  fmtUtc,
  type ExchangeConnectionItem,
  type TradingAccountItem,
} from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorLine,
  Field,
  Modal,
  StatusBadge,
  Table,
  inputClass,
  useAction,
  usePoll,
} from "@/components/ui";

const EMPTY_FORM = {
  name: "",
  exchange: "binance" as "binance" | "okx",
  environment: "demo" as "demo" | "testnet" | "live",
  api_key: "",
  api_secret: "",
  passphrase: "",
  region: "GLOBAL",
  enable_withdraw_permission_simulation: false,
  ip_whitelist_configured: true,
};

export function ExchangesSection() {
  const connections = usePoll<ExchangeConnectionItem[]>(api.connections, 8000);
  const accounts = usePoll<TradingAccountItem[]>(api.accounts, 8000);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const { busy, run } = useAction();

  const submit = async () => {
    const ok = await run(
      "create",
      () =>
        api.createConnection({
          ...form,
          passphrase: form.exchange === "okx" ? form.passphrase || undefined : undefined,
        }),
      "连接已创建（密钥已加密存储，仅显示掩码）"
    );
    if (ok) {
      setOpen(false);
      setForm(EMPTY_FORM);
      connections.reload();
      accounts.reload();
    }
  };

  const accountFor = (connectionId: string) => accounts.data?.find((a) => a.connection_id === connectionId);

  return (
    <div className="space-y-4">
      {/* Security banner */}
      <div className="rounded-lg border border-amber-500/30 bg-amber-500/[0.06] px-4 py-3 text-[11px] leading-relaxed text-amber-200/90">
        <strong className="text-amber-200">安全约束：</strong>
        仅申请 Read + Trade 权限，绝不申请 Withdraw。若检测到提现权限，系统将立即拒绝该连接用于自动交易并记录 CRITICAL 风险事件。
        API Secret 使用主密钥加密存储（macOS Keychain / 本地密钥文件），数据库仅保存密文，前端仅显示掩码。
      </div>

      {connections.error && <ErrorLine error={connections.error} onRetry={connections.reload} />}

      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-200">交易所连接 ({connections.data?.length ?? 0})</h2>
        <Button variant="primary" onClick={() => setOpen(true)}>
          + 新增连接
        </Button>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {(connections.data ?? []).map((c) => {
          const acct = accountFor(c.id);
          return (
            <Card
              key={c.id}
              title={
                <div className="flex items-center gap-2">
                  <span className="uppercase">{c.exchange}</span>
                  <Badge tone={c.environment === "live" ? "bad" : "info"}>{c.environment}</Badge>
                  <StatusBadge status={c.status} />
                </div>
              }
              subtitle={c.name}
              right={
                <div className="flex gap-1.5">
                  <Button size="sm" loading={busy === `test-${c.id}`} onClick={() => run(`test-${c.id}`, () => api.testConnection(c.id), "连接与权限测试完成")}>
                    测试
                  </Button>
                  <Button size="sm" loading={busy === `sync-${c.id}`} onClick={() => run(`sync-${c.id}`, () => api.syncConnection(c.id), "账户已同步并对账")}>
                    同步对账
                  </Button>
                  <Button
                    size="sm"
                    variant="danger"
                    onClick={() => run(`del-${c.id}`, () => api.deleteConnection(c.id), "连接已删除").then(() => connections.reload())}
                  >
                    删除
                  </Button>
                </div>
              }
            >
              <dl className="grid grid-cols-2 gap-x-4 gap-y-2 font-mono text-[11px]">
                <dt className="text-slate-500">API Key</dt>
                <dd className="text-right text-slate-300">{c.api_key_masked}</dd>
                <dt className="text-slate-500">提现权限</dt>
                <dd className="text-right">
                  {c.has_withdraw_permission ? (
                    <Badge tone="bad">检测到 · 已拒绝自动交易</Badge>
                  ) : (
                    <Badge tone="good">无提现权限</Badge>
                  )}
                </dd>
                <dt className="text-slate-500">IP 白名单</dt>
                <dd className="text-right">
                  {c.ip_whitelist_configured ? <Badge tone="good">已配置</Badge> : <Badge tone="warn">未配置 · 建议启用</Badge>}
                </dd>
                <dt className="text-slate-500">最近同步</dt>
                <dd className="text-right text-slate-300">{fmtAgo(c.last_sync_at)}</dd>
                <dt className="text-slate-500">账户对账</dt>
                <dd className="text-right">
                  <StatusBadge status={acct?.sync_status ?? "UNKNOWN"} />
                  {acct?.trading_locked && <Badge tone="bad" className="ml-1">交易锁定</Badge>}
                </dd>
                <dt className="text-slate-500">支持市场</dt>
                <dd className="text-right text-slate-300">
                  {String((acct?.capabilities?.supported_market_types as string[] | undefined)?.join(", ") ?? "spot, usdt_perp")}
                </dd>
              </dl>
              {c.last_error && (
                <div className="mt-2 rounded border border-rose-500/30 bg-rose-500/10 px-2 py-1 text-[10px] text-rose-200">
                  {c.last_error}
                </div>
              )}
            </Card>
          );
        })}
        {!connections.loading && (connections.data?.length ?? 0) === 0 && (
          <Card>
            <Empty>尚未配置交易所连接。</Empty>
          </Card>
        )}
      </div>

      <Card title="账户与产品资格" subtitle="按账户实际返回的能力动态启用，不伪造地区限制" dense>
        <Table
          columns={[
            { key: "exchange", header: "交易所" },
            { key: "env", header: "环境" },
            { key: "region", header: "地区" },
            { key: "sync", header: "对账状态" },
            { key: "lock", header: "交易锁" },
            { key: "reconciled", header: "最近对账" },
            { key: "caps", header: "能力" },
          ]}
          rows={(accounts.data ?? []).map((a) => ({
            exchange: <span className="font-mono uppercase text-slate-200">{a.exchange}</span>,
            env: <Badge tone={a.environment === "live" ? "bad" : "info"}>{a.environment}</Badge>,
            region: <span className="text-slate-400">{a.region}</span>,
            sync: <StatusBadge status={a.sync_status} />,
            lock: a.trading_locked ? <Badge tone="bad">LOCKED</Badge> : <Badge tone="good">OPEN</Badge>,
            reconciled: <span className="font-mono text-slate-500">{fmtUtc(a.last_reconciled_at)}</span>,
            caps: (
              <span className="font-mono text-[10px] text-slate-400">
                {JSON.stringify(a.capabilities?.supported_market_types ?? a.capabilities).slice(0, 60)}
              </span>
            ),
          }))}
          empty="暂无账户"
        />
      </Card>

      <Modal open={open} onClose={() => setOpen(false)} title="新增交易所连接">
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="连接名称">
              <input className={inputClass} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Binance Demo" />
            </Field>
            <Field label="交易所">
              <select className={inputClass} value={form.exchange} onChange={(e) => setForm({ ...form, exchange: e.target.value as "binance" | "okx" })}>
                <option value="binance">Binance</option>
                <option value="okx">OKX</option>
              </select>
            </Field>
            <Field label="环境" hint="Live 需在 Settings 单独解锁并通过前置检查">
              <select
                className={inputClass}
                value={form.environment}
                onChange={(e) => setForm({ ...form, environment: e.target.value as typeof form.environment })}
              >
                <option value="demo">Demo</option>
                <option value="testnet">Testnet</option>
                <option value="live">Live</option>
              </select>
            </Field>
            <Field label="地区">
              <input className={inputClass} value={form.region} onChange={(e) => setForm({ ...form, region: e.target.value })} />
            </Field>
            <Field label="API Key">
              <input className={inputClass} value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} placeholder="只读 + 交易权限" />
            </Field>
            <Field label="API Secret">
              <input type="password" className={inputClass} value={form.api_secret} onChange={(e) => setForm({ ...form, api_secret: e.target.value })} />
            </Field>
            {form.exchange === "okx" && (
              <Field label="Passphrase (OKX 必填)">
                <input type="password" className={inputClass} value={form.passphrase} onChange={(e) => setForm({ ...form, passphrase: e.target.value })} />
              </Field>
            )}
          </div>

          <label className="flex items-start gap-2 rounded border border-terminal-border bg-terminal-panel/60 p-2.5 text-[11px] text-slate-300">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={form.enable_withdraw_permission_simulation}
              onChange={(e) => setForm({ ...form, enable_withdraw_permission_simulation: e.target.checked })}
            />
            <span>
              模拟“API Key 含提现权限”
              <span className="ml-1 text-slate-500">— 用于验证安全拦截：系统将拒绝该连接用于自动交易并记录 CRITICAL 事件。</span>
            </span>
          </label>

          <label className="flex items-start gap-2 rounded border border-terminal-border bg-terminal-panel/60 p-2.5 text-[11px] text-slate-300">
            <input type="checkbox" className="mt-0.5" checked={form.ip_whitelist_configured} onChange={(e) => setForm({ ...form, ip_whitelist_configured: e.target.checked })} />
            <span>已在交易所配置 IP 白名单（动态公网 IP 需定期维护）</span>
          </label>

          <div className="flex justify-end gap-2 pt-1">
            <Button onClick={() => setOpen(false)}>取消</Button>
            <Button variant="primary" onClick={submit} loading={busy === "create"} disabled={!form.name || !form.api_key || !form.api_secret}>
              创建并加密保存
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
