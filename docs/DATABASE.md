# 数据库设计文档 (DATABASE.md)

## 1. 设计准则
1. **精确数值**：所有价格、数量、金额、保证金、手续费、费率字段在 PostgreSQL 中使用 `NUMERIC(28, 10)`，在 Python SQLAlchemy 模型中通过 `DecimalNumeric` 映射为 `decimal.Decimal`。
2. **UTC 时间**：所有时间戳统一存储为 UTC `TIMESTAMPTZ`。
3. **幂等与防重**：
   - `orders`: `UniqueConstraint("account_id", "client_order_id")`
   - `fills`: `UniqueConstraint("exchange", "exchange_trade_id")`
   - `instruments`: `UniqueConstraint("exchange", "market_type", "native_symbol")`
   - `market_candles`: `UniqueConstraint("exchange", "market_type", "symbol", "timeframe", "open_time")`
4. **追加式审计**：订单状态流转、成交明细（`fills`）、风险事件（`risk_events`）、审计日志（`audit_logs`）均为追加记录，不无痕覆盖历史。

## 2. 23 张核心数据表清单
1. `exchange_connections`: 交易所 API 连接、环境标识（`demo`/`testnet`/`live`）、密文引用、提现权限检测状态。
2. `trading_accounts`: 交易账户、所属地区、产品资格（Spot / USDT Perp）与对账状态。
3. `instruments`: 标准化交易对与交易规则（`tick_size`, `lot_size`, `min_qty`, `min_notional`, `contract_size`）。
4. `market_candles`: K 线历史数据及 `is_closed` 标记。
5. `market_snapshots`: 行情快照（`last`, `bid`, `ask`, `mark_price`, `index_price`, `funding_rate`, `is_stale`）。
6. `strategies`: 策略主表与当前激活版本。
7. `strategy_versions`: 策略不可变版本历史（参数 JSON、`code_hash`）。
8. `strategy_runs`: 策略运行实例记录。
9. `agent_runs`: AI Agent 调用记录（模型、Prompt 版本、耗时、Token 与成本）。
10. `ai_decisions`: AI 生成的结构化 `TradePlan` 决策记录。
11. `trade_signals`: 策略与 AI 汇总生成的交易信号（含去重键 `dedup_key` 与过期时间 `expires_at`）。
12. `risk_profiles`: 账户级风控参数配置。
13. `risk_events`: 风控拦截与告警事件记录。
14. `orders`: 委托订单生命周期表（含 `client_order_id`, `status`, `tp_sl_confirmed`）。
15. `fills`: 真实成交明细表（含手续费与已实现盈亏）。
16. `positions`: 当前持仓快照表。
17. `balance_snapshots`: 资产余额快照表。
18. `equity_snapshots`: 账户净值、敞口与分项盈亏快照表。
19. `transfers`: 入金、出金与内部划转记录（与交易盈亏严格剥离）。
20. `audit_logs`: 关键操作前后的 JSON 快照审计日志。
21. `system_events`: 系统组件健康与故障事件表。
22. `backtest_runs`: 历史回测配置、样本区间与绩效结果表。
23. `kill_switch_states`: 全局或账户级紧急熔断开关状态表。
24. `journal_entries`: 交易全链路复盘记录表（关联信号、AI 决策、风控、订单、成交与复盘摘要）。
