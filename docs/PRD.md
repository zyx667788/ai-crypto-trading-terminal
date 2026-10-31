# 产品需求文档 (PRD.md)

## 1. 产品定位
本机运行的个人 AI 加密货币交易终端（Web 端访问 `http://localhost:3000`），面向单一本地管理员用户（预留 `owner_id`），统一管理 Binance 与 OKX 的现货（Spot）与 USDT 本位永续合约（USDT Perpetual）。

## 2. 功能优先级与清单

### P0（V1.0 必选核心闭环）
1. **Dashboard 总览**：总权益、Binance/OKX 分账户权益、现货估值、合约保证金、已实现/未实现盈亏、手续费与资金费率收支、当日与累计盈亏、持仓与挂单数、运行策略与 Agent 状态、数据新鲜度（`is_stale`）与最后更新时间。入金/出金/内部转账不计入交易利润。
2. **交易所连接管理**：支持 Binance、OKX，区分 `demo`/`testnet` 与 `live` 环境。支持连接测试、权限校验（拒绝含提现 `withdraw` 权限的 API Key）、账户产品能力检查、密钥本机加密与掩码展示。
3. **市场行情（Market Data）**：支持 `canonical_symbol`（如 `BTC/USDT`、`BTC/USDT:USDT`）与交易所 `native_symbol` 双向映射；区分 Last Price、Mark Price、Index Price；支持 K 线（`1m, 5m, 15m, 1h, 4h, 1d`，默认策略仅使用 `is_closed=True` 已收盘 K 线）、订单簿深度摘要、资金费率及交易精度规则（`tick_size`, `lot_size`, `min_qty`, `min_notional`, `contract_size`）。
4. **Market Scanner**：支持配置扫描范围與过滤条件（成交额、价差、波动率、成交量变化），计算 RSI、MACD、EMA、ATR、趋势强度、盘口不平衡度与资金费率，输出候选机会并决定是否进入 AI 分析队列。
5. **Strategy Engine**：内置可解释基准策略（1. EMA+ATR+成交量趋势跟随策略；2. 区间高低点+成交量+波动率突破策略）。支持唯一 ID、不可覆盖的版本管理（每次修改参数生成新版本与 `code_hash`）、冷却时间与信号去重。
6. **AI Agent 工作流**：由状态机/工作流协调 `Market Agent`、`Scanner Agent`、`Strategy Agent`、`Risk Agent`、`Journal Agent`。AI 输出严格校验为 `TradePlan` Pydantic 模型，绝不接触 API Secret，也不直接下单。
7. **Risk Engine（最高优先级）**：10 条确定性硬规则，使用 `Decimal` 精确计算风险预算与仓位数量，支持账户/全局 Kill Switch 熔断，异常时 Fail-Closed。
8. **Execution Engine**：完整订单状态机（`CREATED -> VALIDATED -> SUBMITTED -> ACKNOWLEDGED -> PARTIALLY_FILLED -> FILLED` 及 `REJECTED / CANCELED / EXPIRED / UNKNOWN`），`client_order_id` 唯一约束与超时查询对账机制，验证原生止盈止损保护单。
9. **Portfolio、Positions 与对账**：定时与事件驱动同步余额、订单、成交与持仓，识别本地与交易所状态差异并在不一致时锁定新开仓。

### P1（V1.0 分析、复盘与回测）
1. **Analytics & Journal**：净值曲线（Equity Curve）、胜率（Win Rate）、盈亏比（Profit Factor）、最大回撤（Max Drawdown）、Sharpe Ratio、按策略/交易所统计，以及完整记录信号->AI决策->风控->订单->成交->复盘摘要的 Journal。
2. **Backtest 回测引擎**：支持历史已收盘 K 线回测，计入手续费、滑点与永续资金费率，区分训练/验证与样本外区间，杜绝前视偏差。
