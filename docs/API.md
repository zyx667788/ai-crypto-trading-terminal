# 内部 API 与 WebSocket 规范 (API.md)

统一 REST 前缀：`/api/v1`

## 1. 交易所与账户 (Exchanges & Accounts)
- `GET /api/v1/exchanges` - 支持的交易所与默认产品能力说明
- `GET /api/v1/exchange-connections` - 已配置的连接列表（仅返回掩码密钥）
- `POST /api/v1/exchange-connections` - 新增交易所连接（自动加密存储密钥）
- `POST /api/v1/exchange-connections/{id}/test` - 测试连接、检查权限（拦截提现权限）与产品资格
- `GET /api/v1/exchange-connections/{id}/status` - 查询连接健康与最近同步状态
- `POST /api/v1/exchange-connections/{id}/sync` - 触发账户余额、持仓与订单对账同步
- `DELETE /api/v1/exchange-connections/{id}` - 停用并删除连接
- `GET /api/v1/accounts` - 交易账户列表
- `GET /api/v1/accounts/{id}/balances` - 账户资产余额明细
- `GET /api/v1/accounts/{id}/capabilities` - 账户地区与支持的产品能力（Spot / USDT Perp）

## 2. 行情与 Scanner (Markets & Scanner)
- `GET /api/v1/markets/instruments` - 交易对与精度规则列表
- `GET /api/v1/markets/tickers` - 最新行情快照（含 Last / Mark / Index Price 与 `is_stale`）
- `GET /api/v1/markets/candles` - K 线数据（默认标记 `is_closed`）
- `GET /api/v1/markets/orderbook` - 订单簿深度摘要与不平衡度
- `GET /api/v1/markets/funding-rate` - 永续合约资金费率与下次结算时间
- `GET /api/v1/scanner/status` - Scanner 运行状态与配置
- `GET /api/v1/scanner/opportunities` - 扫描发现的候选交易机会
- `GET /api/v1/scanner/rankings` - 候选机会综合评分排名
- `POST /api/v1/scanner/start` - 启动/立即执行一轮市场扫描
- `POST /api/v1/scanner/pause` - 暂停市场扫描

## 3. 策略与 AI 系统 (Strategies & AI Agents)
- `GET /api/v1/strategies` - 策略列表与当前版本
- `POST /api/v1/strategies` - 创建策略
- `GET /api/v1/strategies/{id}` - 策略详情与版本历史
- `PUT /api/v1/strategies/{id}` - 更新策略配置（自动生成不可变新版本）
- `POST /api/v1/strategies/{id}/start` - 启动策略并生成信号
- `POST /api/v1/strategies/{id}/pause` - 暂停策略
- `GET /api/v1/agents/status` - 5 大 AI Agent 状态与模型配置
- `GET /api/v1/agents/runs` - Agent 执行历史与 Token/耗时统计
- `GET /api/v1/decisions` - AI 生成的 `TradePlan` 决策记录
- `POST /api/v1/decisions/generate` - 触发 AI 对候选信号/交易对生成结构化 `TradePlan`
- `GET /api/v1/trade-signals` - 策略信号列表

## 4. 风控、订单与持仓 (Risk, Orders & Positions)
- `GET /api/v1/risk/profile` - 获取当前风控参数配置
- `PUT /api/v1/risk/profile` - 修改风控参数（记录审计日志）
- `GET /api/v1/risk/status` - 当前风控状态、日亏损、敞口、熔断状态
- `GET /api/v1/risk/events` - 风控拦截与告警事件列表
- `POST /api/v1/risk/kill-switch` - 触发紧急熔断（禁止一切新开仓）
- `POST /api/v1/risk/resume` - 用户显式确认解除熔断
- `POST /api/v1/risk/flatten` - 紧急全部平仓（Reduce-Only）
- `POST /api/v1/orders` - 提交交易计划/订单（强制经过 Risk Engine 审核）
- `GET /api/v1/orders` - 订单列表
- `GET /api/v1/orders/{id}` - 订单详情与成交明细
- `POST /api/v1/orders/{id}/cancel` - 撤销未成交订单
- `POST /api/v1/orders/{id}/reconcile` - 针对 `UNKNOWN` 或挂单执行单笔查询对账
- `GET /api/v1/fills` - 真实成交记录列表
- `GET /api/v1/positions` - 当前持仓列表
- `POST /api/v1/positions/{id}/close` - 请求平仓
- `GET /api/v1/portfolio/summary` - Dashboard 资产与盈亏总览

## 5. 自动化、分析、复盘与回测 (Automation, Analytics, Journal & Backtests)
- `GET /api/v1/automation/status` - 从数据库读取 Automation Control、Recovery Checkpoint 与各进程 Worker Heartbeat；`order_gate` 字段与 Worker 使用**同一个**只读闸门决策
- `POST /api/v1/automation/start` - 启动自动交易闭环；`AUTO_TRADING_ENABLED=false` 时返回 403，Live 仍需前置检查与二次确认
- `POST /api/v1/automation/pause` - 持久化暂停自动新开仓，所有 Worker 进程下一轮立即生效
- `POST /api/v1/automation/run-cycle` - **请求体** `{simulate_ai_invalid_json?, simulate_ai_timeout?, simulate_order_timeout?, confirm?}`；执行“扫描 -> 策略信号 -> AI TradePlan -> Outbox 入队 -> 复盘”；**不在 API 进程内下单**，订单任务由 Execution Worker 消费
- `GET /api/v1/analytics/summary` - 综合绩效统计（胜率、盈亏比、夏普、最大回撤、费用占比）
- `GET /api/v1/analytics/equity-curve` - 账户净值曲线序列
- `GET /api/v1/analytics/strategy-performance` - 各策略与各交易所维度表现对比
- `GET /api/v1/journal` - 交易复盘日志列表
- `POST /api/v1/backtests` - 创建并运行历史回测（含手续费、滑点、资金费率与样本外切分）
- `GET /api/v1/backtests` - 回测任务列表
- `GET /api/v1/backtests/{id}` - 回测详细报告

## 6. 管理接口防护 (Admin API protection — V1.2.1)

所有 `POST / PUT / PATCH / DELETE /api/v1/*`（健康检查除外）都经过同一层校验：

1. **Host**：只接受 `127.0.0.1`、`localhost`、`::1`（任意端口）；其它 Host 直接 403。
2. **Sec-Fetch-Site**：为 `cross-site` 时 403 `{"detail":"Cross-site request blocked"}`。
3. **Origin**：存在时必须精确匹配 `ADMIN_ALLOWED_ORIGINS`（逗号分隔），否则 403 `{"detail":"Origin not allowed"}`。
4. **X-Admin-Token**：当 `ADMIN_API_TOKEN` 非空时必填，常量时间比对。

既无 `Origin` 也无 `Sec-Fetch-Site` 的调用方（curl / CLI / 测试客户端）被视为非浏览器本地客户端放行。
前端在 `NEXT_PUBLIC_ADMIN_TOKEN` 存在时会自动附加 `X-Admin-Token`。

受影响的状态变更接口包括：`/automation/{start,pause,run-cycle}`、`/system/recovery/{run,acknowledge}`、`/risk/{kill-switch,resume,reset-daily-locks,flatten}`。

**Recovery 人工确认**：`POST /api/v1/system/recovery/acknowledge`
```json
{ "reason": "operator reviewed the incident log", "confirm": true }
```
`reason` 至少 10 字符。该接口**不是**绕过：它只解除操作者自己声明的阻断，随后重新执行技术校验（数据库健康、UNKNOWN 订单、未保护持仓、本地/交易所对账），全部通过才置 READY，否则保持 `RECOVERY_MODE` 并在 `new_entries_blocked_due_to` 中给出原因。

## 7. WebSocket 实时通道
- `/ws/dashboard`, `/ws/market`, `/ws/scanner`, `/ws/agents`, `/ws/orders`, `/ws/positions`, `/ws/risk`, `/ws/system`
- 统一信封格式：
```json
{
  "event_id": "uuid",
  "event_type": "order.updated",
  "occurred_at": "2026-01-01T00:00:00Z",
  "source": "binance",
  "data": {}
}
```
