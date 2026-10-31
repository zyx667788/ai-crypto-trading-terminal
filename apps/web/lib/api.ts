/**
 * Typed API client for the AI Crypto Trading Terminal.
 *
 * Design rule: this client NEVER fabricates data. If a request fails the caller
 * receives a thrown `ApiError`, and the UI must render "数据过期/未知" (stale/unknown)
 * rather than a plausible-looking value. Fabricated prices would violate the
 * product's core requirement that stale data must never masquerade as live data.
 */

/**
 * Root origin of the API. Paths below already carry the `/api/v1` prefix, so any
 * trailing `/api/v1` in the environment value is stripped to avoid `/api/v1/api/v1`.
 */
export const API_ROOT = (process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000")
  .replace(/\/+$/, "")
  .replace(/\/api\/v1$/, "")

export const WS_BASE =
  process.env.NEXT_PUBLIC_WS_URL?.replace(/\/$/, "") || "ws://127.0.0.1:8000";

export type ExchangeName = "binance" | "okx";
export type MarketType = "spot" | "usdt_perp";
export type TradingEnvironment = "demo" | "testnet" | "live";
export type Direction = "LONG" | "SHORT" | "BUY" | "SELL";
export type Timeframe = "1m" | "5m" | "15m" | "1h" | "4h" | "1d";

export type OrderStatus =
  | "CREATED"
  | "VALIDATED"
  | "SUBMITTED"
  | "ACKNOWLEDGED"
  | "PARTIALLY_FILLED"
  | "FILLED"
  | "REJECTED"
  | "CANCELED"
  | "EXPIRED"
  | "UNKNOWN";

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  const isMutating = ["POST", "PUT", "PATCH", "DELETE"].includes((init?.method || "GET").toUpperCase());
  const adminToken = process.env.NEXT_PUBLIC_ADMIN_TOKEN;
  // Sec-Fetch-Site / Origin are set by the browser and cannot be spoofed from JS
  // (they are forbidden headers). The backend relies on the real values, so we only
  // add the optional admin token here.
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(isMutating && adminToken ? { "X-Admin-Token": adminToken } : {}),
    ...((init?.headers as Record<string, string>) || {}),
  };
  try {
    res = await fetch(`${API_ROOT}${path}`, {
      ...init,
      cache: "no-store",
      credentials: "same-origin",
      headers,
    });
  } catch (err) {
    throw new ApiError(0, err, "API unreachable — data status unknown (stale).");
  }
  const text = await res.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!res.ok) {
    throw new ApiError(res.status, body, `Request failed (${res.status})`);
  }
  return body as T;
}

const get = <T>(path: string) => request<T>(path);
const post = <T>(path: string, payload?: unknown) =>
  request<T>(path, { method: "POST", body: payload === undefined ? undefined : JSON.stringify(payload) });
const put = <T>(path: string, payload: unknown) =>
  request<T>(path, { method: "PUT", body: JSON.stringify(payload) });
const del = <T>(path: string) => request<T>(path, { method: "DELETE" });

/* ------------------------------------------------------------------ types */
export interface HealthPayload {
  status: string;
  service?: string;
  version?: string;
  database?: { name: string; status: string; latency_ms?: number };
  cache?: { name: string; status: string; backend?: string; detail?: string };
  recovery_state?: string;
  trading_allowed?: boolean;
  live_trading_enabled?: boolean;
  checked_at?: string;
}

export interface ExchangeConnectionItem {
  id: string;
  name: string;
  exchange: ExchangeName;
  environment: TradingEnvironment;
  api_key_masked: string;
  status: string;
  has_withdraw_permission: boolean;
  ip_whitelist_configured: boolean;
  permissions_json: Record<string, unknown>;
  last_sync_at: string | null;
  last_error: string | null;
  created_at: string;
}

export interface TradingAccountItem {
  id: string;
  connection_id: string;
  exchange: ExchangeName;
  environment: string;
  account_type: string;
  region: string;
  capabilities: Record<string, unknown>;
  is_active: boolean;
  trading_locked: boolean;
  lock_reason: string | null;
  sync_status: string;
  last_reconciled_at: string | null;
}

export interface InstrumentItem {
  id: string;
  exchange: ExchangeName;
  market_type: MarketType;
  native_symbol: string;
  canonical_symbol: string;
  base_asset: string;
  quote_asset: string;
  tick_size: string;
  lot_size: string;
  min_qty: string;
  min_notional: string;
  contract_size: string;
  max_leverage: string;
  status: string;
}

export interface TickerItem {
  exchange: ExchangeName;
  market_type: MarketType;
  symbol: string;
  event_time: string;
  received_at: string;
  last: string;
  bid: string;
  ask: string;
  spread_bps: string;
  volume_24h: string;
  quote_volume_24h: string;
  change_24h_pct: string;
  mark_price: string | null;
  index_price: string | null;
  funding_rate: string | null;
  next_funding_time: string | null;
  orderbook_summary: Record<string, unknown>;
  indicators: Record<string, unknown>;
  is_stale: boolean;
}

export interface CandleItem {
  exchange: string;
  market_type: string;
  symbol: string;
  timeframe: string;
  open_time: string;
  close_time: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
  is_closed: boolean;
}

export interface OpportunityItem {
  symbol: string;
  exchange: string;
  market_type: string;
  timeframe: string;
  score: number | string;
  direction_bias: string;
  trigger_reasons: string[];
  indicators_snapshot: Record<string, unknown>;
  risk_filter_passed: boolean;
  risk_filter_reason?: string;
  enter_ai_queue: boolean;
  scanned_at: string;
}

export interface StrategyItem {
  id: string;
  code: string;
  name: string;
  description: string;
  exchange: string;
  market_type: string;
  symbols: string[];
  timeframe: string;
  status: string;
  current_version: number;
  cooldown_seconds: number;
  max_positions: number;
  code_hash?: string;
  parameters?: Record<string, unknown>;
  entry_conditions?: Record<string, unknown>;
  exit_conditions?: Record<string, unknown>;
  risk_parameters?: Record<string, unknown>;
  versions?: Array<{
    id?: string;
    version: number;
    code_hash: string;
    created_at: string;
    change_note?: string;
    parameters: Record<string, unknown>;
  }>;
}

export interface TradeSignalItem {
  id: string;
  symbol: string;
  exchange: string;
  market_type: string;
  timeframe: string;
  direction: string;
  trigger_reason: string;
  score: string;
  status: string;
  expires_at: string;
  created_at: string;
  indicators_snapshot?: Record<string, unknown>;
}

export interface AgentStatusPayload {
  agents: Array<{
    name?: string;
    role?: string;
    run_type?: string;
    status: string;
    model?: string;
    description?: string;
    last_run_at?: string | null;
    latency_ms?: number;
    tokens?: number;
  }>;
  [key: string]: unknown;
}

export interface AgentRunItem {
  id: string;
  run_type: string;
  model: string;
  provider: string;
  status: string;
  latency_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
  estimated_cost_usd: string;
  created_at: string;
  error_message?: string | null;
}

export interface TradePlanPayload {
  exchange: ExchangeName;
  market_type: MarketType;
  symbol: string;
  direction: Direction;
  entry_type: "MARKET" | "LIMIT";
  entry_price: string | null;
  entry_zone?: string[] | null;
  stop_loss: string;
  take_profit: string;
  confidence: string;
  timeframe: Timeframe;
  thesis: string;
  invalidation_conditions: string[];
  expires_at: string;
  strategy_version: string;
}

export interface DecisionItem {
  id: string;
  symbol: string;
  exchange: string;
  market_type: string;
  direction: string;
  confidence: string;
  output_json: Record<string, unknown>;
  risk_explanation: string;
  is_valid: boolean;
  validation_error: string | null;
  created_at: string;
}

export interface RiskLimits {
  max_risk_per_trade_pct: string;
  max_daily_loss_pct: string;
  max_total_exposure_pct: string;
  max_symbol_exposure_pct: string;
  max_open_positions: number;
  max_leverage: string;
  max_slippage_bps: string;
  max_spread_bps: string;
  max_data_stale_seconds: number;
  max_consecutive_api_failures: number;
  max_consecutive_losses: number;
  max_daily_trades: number;
  account_paused: boolean;
}

export interface RiskEventItem {
  id: string;
  account_id: string;
  event_type: string;
  rule_code: string;
  severity: string;
  details: Record<string, unknown>;
  created_at: string;
}

export interface OrderItem {
  id: string;
  account_id: string;
  exchange: string;
  environment: string;
  market_type: string;
  symbol: string;
  client_order_id: string;
  exchange_order_id: string | null;
  order_type: string;
  side: string;
  position_side: string;
  reduce_only: boolean;
  status: OrderStatus;
  qty: string;
  filled_qty: string;
  price: string;
  avg_fill_price: string;
  stop_loss: string | null;
  take_profit: string | null;
  tp_sl_confirmed: boolean;
  protection_order_ids: string[];
  reject_reason: string | null;
  created_at: string;
}

export interface FillItem {
  id: string;
  order_id: string;
  exchange: string;
  symbol: string;
  side: string;
  price: string;
  qty: string;
  fee: string;
  fee_currency: string;
  realized_pnl: string;
  occurred_at: string;
}

export interface PositionItem {
  id: string;
  account_id: string;
  exchange: string;
  environment: string;
  market_type: string;
  symbol: string;
  position_side: string;
  qty: string;
  entry_price: string;
  mark_price: string;
  liquidation_price: string | null;
  leverage: string;
  margin_used: string;
  notional_value: string;
  unrealized_pnl: string;
  realized_pnl: string;
  stop_loss: string | null;
  take_profit: string | null;
  tp_sl_confirmed: boolean;
  status: string;
  updated_at: string;
}

export interface PortfolioSummary {
  currency: string;
  total_equity: string;
  available_balance: string;
  margin_balance: string;
  notional_exposure: string;
  spot_valuation: string;
  perp_margin: string;
  realized_pnl: string;
  unrealized_pnl: string;
  fees_paid: string;
  funding_pnl: string;
  net_transfers: string;
  net_profit: string;
  transfers_note: string;
  per_exchange: Record<string, Record<string, string>>;
  open_positions_count: number;
  open_orders_count: number;
  unknown_orders_count: number;
  active_strategies: Array<{ id: string; name: string; version: number }>;
  active_signals_count: number;
  agent_status: { last_run_type: string | null; last_run_status: string; last_run_at: string | null };
  data_freshness: {
    last_updated_at: string | null;
    data_age_seconds: number | null;
    is_stale: boolean;
    accounts_not_synced: string[];
    label: string;
  };
  generated_at: string;
}

export interface RiskStatusPayload {
  status?: string;
  kill_switch?: { enabled: boolean; scope?: string; reason?: string };
  kill_switch_enabled?: boolean;
  daily_realized_loss?: string;
  daily_unrealized_loss?: string;
  daily_trades_count?: number;
  consecutive_losses?: number;
  open_positions_count?: number;
  total_exposure?: string;
  equity?: string;
  limits?: RiskLimits;
  [key: string]: unknown;
}

export interface AutomationStatusPayload {
  running: boolean;
  paused: boolean;
  environment: string;
  live_trading_enabled: boolean;
  last_cycle_at: string | null;
  last_cycle_summary: Record<string, unknown> | null;
  cycles_executed: number;
  pipeline: string[];
  live_preflight: { ready: boolean; items: Array<{ item: string; passed: boolean }>; note: string };
  recovery?: RecoveryStatusPayload;
  workers?: Array<{
    name: string;
    role: string;
    description: string;
    last_run_at: string | null;
    last_status: string;
    healthy: boolean;
    memory_budget_mb: number;
  }>;
}

export interface RecoveryStatusPayload {
  state: string;
  ready: boolean;
  new_entries_blocked: boolean;
  new_entries_blocked_due_to: string[];
  steps: Array<{ state: string; ok: boolean; detail: string; at: string }>;
  counts: { mismatches: number; unknown_orders: number; unprotected_positions: number };
  policy: Record<string, unknown>;
}

export interface AnalyticsSummaryPayload {
  total_equity: string;
  realized_pnl: string;
  unrealized_pnl: string;
  fees_paid: string;
  funding_pnl: string;
  net_profit: string;
  total_trades: number;
  win_rate: string;
  profit_factor: string;
  max_drawdown_pct: string;
  sharpe_ratio: string;
  sharpe_note: string;
  avg_win: string;
  avg_loss: string;
  strategy_performance: Record<string, { trades: number; net_pnl: string; fees: string; win_rate: string }>;
  exchange_performance: Record<string, { fills: number; volume: string; fees: string; realized_pnl: string }>;
  generated_at: string;
}

export interface EquityPoint {
  observed_at: string;
  equity: string;
  realized_pnl: string;
  unrealized_pnl: string;
  notional_exposure: string;
  account_id: string;
}

export interface BacktestItem {
  id: string;
  strategy_version_id: string;
  status: string;
  dataset: Record<string, unknown>;
  parameters: Record<string, unknown>;
  metrics: Record<string, Record<string, string | number>>;
  equity_curve?: Array<{ at: string; equity: string }>;
  trades?: Array<Record<string, string>>;
  created_at: string;
  disclaimer?: string;
}

export interface JournalItem {
  id: string;
  symbol: string;
  exchange: string;
  market_type: string;
  direction: string;
  strategy_version: string;
  ai_model_version: string;
  ai_output: Record<string, unknown>;
  risk_check: Record<string, unknown>;
  order_request: Record<string, unknown>;
  exchange_order_id: string | null;
  fills: Array<Record<string, unknown>>;
  fees_paid: string;
  funding_pnl: string;
  close_reason: string;
  net_realized_pnl: string;
  review_summary: string;
  created_at: string;
  chain: Array<{ step: string; ref: string | null }>;
}

export interface SystemStatusPayload {
  liveness: {
    status: string;
    pid: number;
    process_uptime_seconds: number;
    host_uptime_seconds: number;
    checked_at: string;
  };
  readiness: {
    status: string;
    trading_allowed: boolean;
    checks: Record<string, boolean>;
    new_entries_blocked_due_to: string[];
  };
  resource: {
    host: Record<string, string>;
    memory: {
      total_mb: number;
      used_mb: number;
      available_mb: number;
      usage_pct: number;
      swap_used_mb?: number;
      pressure_level: string;
      degradation_guidance: string;
      unified_16gb_budget_mb: Record<string, number>;
      budget_total_mb: number;
    };
    disk: { total_gb: number; free_gb: number; free_pct: number; status: string };
    cpu_pct: number;
    observed_at: string;
  };
  alerts: Array<{ level: string; code: string; message: string; raised_at: string }>;
  backups: { count: number; latest: Record<string, unknown> | null; latest_age_hours: number | null; stale: boolean; retention: string };
  notification_channels: Record<string, boolean>;
  external_heartbeat: { configured: boolean; last_sent_at: string | null; url_masked: string | null };
}

/* ----------------------------------------------------------------- client */
export const api = {
  health: () => get<HealthPayload>("/health"),
  systemStatus: () => get<SystemStatusPayload>("/api/v1/system/status"),
  publicConfig: () => get<Record<string, unknown>>("/api/v1/system/config"),
  systemEvents: (limit = 50) => get<Array<Record<string, unknown>>>(`/api/v1/system/events?limit=${limit}`),
  rateLimits: () => get<Record<string, unknown>>("/api/v1/system/rate-limits"),
  alerts: () => get<Array<Record<string, unknown>>>("/api/v1/system/alerts"),
  backups: () => get<Record<string, unknown>>("/api/v1/system/backups"),
  createBackup: (tag = "manual") => post<Record<string, unknown>>(`/api/v1/system/backups?tag=${tag}`),
  restoreCheck: () => post<Record<string, unknown>>("/api/v1/system/backups/restore-check"),
  recovery: () => get<RecoveryStatusPayload>("/api/v1/system/recovery"),
  runRecovery: () => post<RecoveryStatusPayload>("/api/v1/system/recovery/run?force=true"),
  acknowledgeRecovery: (reason = "Operator reviewed reconciliation and protective orders", confirm = true) =>
    post<RecoveryStatusPayload>("/api/v1/system/recovery/acknowledge", { reason, confirm }),

  exchanges: () => get<Array<Record<string, unknown>>>("/api/v1/exchanges"),
  connections: () => get<ExchangeConnectionItem[]>("/api/v1/exchange-connections"),
  createConnection: (payload: Record<string, unknown>) =>
    post<ExchangeConnectionItem>("/api/v1/exchange-connections", payload),
  testConnection: (id: string) => post<Record<string, unknown>>(`/api/v1/exchange-connections/${id}/test`),
  syncConnection: (id: string) => post<Record<string, unknown>>(`/api/v1/exchange-connections/${id}/sync`),
  deleteConnection: (id: string) => del<Record<string, unknown>>(`/api/v1/exchange-connections/${id}`),
  accounts: () => get<TradingAccountItem[]>("/api/v1/accounts"),
  balances: (id: string) => get<Array<Record<string, unknown>>>(`/api/v1/accounts/${id}/balances`),
  capabilities: (id: string) => get<Record<string, unknown>>(`/api/v1/accounts/${id}/capabilities`),

  instruments: () => get<InstrumentItem[]>("/api/v1/markets/instruments"),
  tickers: () => get<TickerItem[]>("/api/v1/markets/tickers"),
  candles: (params: { exchange: string; symbol: string; timeframe: string; limit?: number; market_type?: string }) => {
    const q = new URLSearchParams({
      exchange: params.exchange,
      symbol: params.symbol,
      timeframe: params.timeframe,
      limit: String(params.limit ?? 120),
      ...(params.market_type ? { market_type: params.market_type } : {}),
    });
    return get<CandleItem[]>(`/api/v1/markets/candles?${q}`);
  },
  orderbook: (params: { exchange: string; symbol: string; market_type?: string }) => {
    const q = new URLSearchParams({
      exchange: params.exchange,
      symbol: params.symbol,
      depth: "10",
      ...(params.market_type ? { market_type: params.market_type } : {}),
    });
    return get<Record<string, unknown>>(`/api/v1/markets/orderbook?${q}`);
  },
  fundingRate: (params: { exchange: string; symbol: string }) =>
    get<Record<string, unknown>>(
      `/api/v1/markets/funding-rate?exchange=${params.exchange}&symbol=${params.symbol}`
    ),

  scannerStatus: () => get<Record<string, unknown>>("/api/v1/scanner/status"),
  opportunities: () => get<OpportunityItem[]>("/api/v1/scanner/opportunities"),
  rankings: () => get<OpportunityItem[]>("/api/v1/scanner/rankings"),
  startScanner: () => post<Record<string, unknown>>("/api/v1/scanner/start"),
  pauseScanner: () => post<Record<string, unknown>>("/api/v1/scanner/pause"),

  strategies: () => get<StrategyItem[]>("/api/v1/strategies"),
  strategy: (id: string) => get<StrategyItem>(`/api/v1/strategies/${id}`),
  updateStrategy: (id: string, payload: Record<string, unknown>) =>
    put<StrategyItem>(`/api/v1/strategies/${id}`, payload),
  startStrategy: (id: string) => post<Record<string, unknown>>(`/api/v1/strategies/${id}/start`),
  pauseStrategy: (id: string) => post<Record<string, unknown>>(`/api/v1/strategies/${id}/pause`),
  signals: () => get<TradeSignalItem[]>("/api/v1/trade-signals"),

  agentStatus: () => get<AgentStatusPayload>("/api/v1/agents/status"),
  aiProviderStatus: () => get<Record<string, unknown>>("/api/v1/agents/provider"),
  testAiProvider: () => post<Record<string, unknown>>("/api/v1/agents/provider/test"),
  agentRuns: () => get<AgentRunItem[]>("/api/v1/agents/runs"),
  decisions: () => get<DecisionItem[]>("/api/v1/decisions"),
  generateDecision: (opts?: { simulate_invalid_json?: boolean; simulate_timeout?: boolean }) => {
    const q = new URLSearchParams();
    if (opts?.simulate_invalid_json) q.set("simulate_invalid_json", "true");
    if (opts?.simulate_timeout) q.set("simulate_timeout", "true");
    return post<{ status: string; trade_plan: Record<string, unknown> | null; message?: string }>(
      `/api/v1/decisions/generate${q.toString() ? `?${q}` : ""}`
    );
  },

  riskProfile: () => get<{ account_id: string; enabled: boolean; limits: RiskLimits }>("/api/v1/risk/profile"),
  updateRiskProfile: (limits: Partial<RiskLimits>) => put<Record<string, unknown>>("/api/v1/risk/profile", limits),
  riskStatus: () => get<RiskStatusPayload>("/api/v1/risk/status"),
  riskEvents: () => get<RiskEventItem[]>("/api/v1/risk/events"),
  killSwitch: (reason: string) =>
    post<Record<string, unknown>>("/api/v1/risk/kill-switch", { scope: "global", reason }),
  resume: (code: string) =>
    post<Record<string, unknown>>("/api/v1/risk/resume", {
      scope: "global",
      confirmation_code: code,
      reason: "Operator verified state",
    }),
  resetDailyLocks: (code: string, reason = "Operator acknowledged the loss streak and reset daily counters") =>
    post<Record<string, unknown>>("/api/v1/risk/reset-daily-locks", {
      confirmation_code: code,
      reason,
    }),
  flatten: (accountId?: string) =>
    post<Record<string, unknown>>(`/api/v1/risk/flatten${accountId ? `?account_id=${accountId}` : ""}`),

  orders: (status?: string) => get<OrderItem[]>(`/api/v1/orders${status ? `?status=${status}` : ""}`),
  submitOrder: (plan: TradePlanPayload, opts?: { signal_id?: string; decision_id?: string; simulate_timeout?: boolean }) =>
    post<Record<string, unknown>>("/api/v1/orders", { trade_plan: plan, ...opts }),
  cancelOrder: (id: string) => post<Record<string, unknown>>(`/api/v1/orders/${id}/cancel`),
  reconcileOrder: (id: string, remote = "FILLED") =>
    post<Record<string, unknown>>(`/api/v1/orders/${id}/reconcile?simulated_remote_status=${remote}`),
  fills: () => get<FillItem[]>("/api/v1/fills"),
  positions: () => get<PositionItem[]>("/api/v1/positions?status=ALL"),
  closePosition: (id: string) => post<Record<string, unknown>>(`/api/v1/positions/${id}/close`),
  portfolio: () => get<PortfolioSummary>("/api/v1/portfolio/summary"),
  reconcilePortfolio: () => post<Record<string, unknown>>("/api/v1/portfolio/reconcile"),

  automationStatus: () => get<AutomationStatusPayload>("/api/v1/automation/status"),
  startAutomation: (environment: string = "demo") =>
    post<Record<string, unknown>>("/api/v1/automation/start", { environment }),
  pauseAutomation: () => post<Record<string, unknown>>("/api/v1/automation/pause"),
  runCycle: (opts?: {
    simulate_ai_invalid_json?: boolean;
    simulate_ai_timeout?: boolean;
    simulate_order_timeout?: boolean;
    confirm?: boolean;
  }) => {
    return post<Record<string, unknown>>("/api/v1/automation/run-cycle", {
      simulate_ai_invalid_json: Boolean(opts?.simulate_ai_invalid_json),
      simulate_ai_timeout: Boolean(opts?.simulate_ai_timeout),
      simulate_order_timeout: Boolean(opts?.simulate_order_timeout),
      confirm: opts?.confirm ?? true,
    });
  },

  analytics: () => get<AnalyticsSummaryPayload>("/api/v1/analytics/summary"),
  equityCurve: () => get<EquityPoint[]>("/api/v1/analytics/equity-curve"),
  strategyPerformance: () => get<Record<string, unknown>>("/api/v1/analytics/strategy-performance"),
  journal: () => get<JournalItem[]>("/api/v1/journal"),
  backtests: () => get<BacktestItem[]>("/api/v1/backtests"),
  runBacktest: (payload: Record<string, unknown>) => post<BacktestItem>("/api/v1/backtests", payload),
};

/* ---------------------------------------------------------------- helpers */
export function fmtUsd(value: unknown, decimals = 2): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

export function fmtSigned(value: unknown, decimals = 2): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  const text = fmtUsd(Math.abs(n), decimals);
  return `${n > 0 ? "+" : n < 0 ? "-" : ""}${text}`;
}

export function fmtNum(value: unknown, decimals = 4): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("en-US", { maximumFractionDigits: decimals });
}

export function fmtPct(value: unknown, decimals = 2): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${n.toFixed(decimals)}%`;
}

export function fmtRate(value: unknown, decimals = 4): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(decimals)}%`;
}

export function fmtUtc(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return `${d.toISOString().slice(0, 19).replace("T", " ")}Z`;
}

export function fmtAgo(iso: string | null | undefined): string {
  if (!iso) return "never";
  const d = new Date(iso).getTime();
  if (Number.isNaN(d)) return "never";
  const s = Math.max(0, Math.floor((Date.now() - d) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
