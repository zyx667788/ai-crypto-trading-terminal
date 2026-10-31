# 测试计划与执行记录 (TEST_PLAN.md)

本项目采取分层测试策略（单元测试、集成测试、故障注入测试），所有与交易资金、价格、数量计算相关的逻辑均遵循严格的 `Decimal` 精度标准与 Fail-Closed 原则。

---

## 1. 单元测试 (Unit Tests)
- `Decimal` 仓位计算：现货与 USDT 永续合约（含 `contract_size` 乘数）仓位换算、手续费与滑点缓冲。
- 精度舍入：`tick_size` 价格步长与 `lot_size` 数量步长向下舍入、`min_qty` 与 `min_notional` 校验。
- 10 大硬性风控规则：止损方向错误拦截、零止损距离拦截、数据过期拦截、日亏损超限拦截、最大杠杆与最大敞口拦截、Kill Switch 熔断拦截、风控异常 Fail-Closed。
- AI Schema 校验：合法 `TradePlan` 通过、缺失字段/方向矛盾/过期时间非法/非法 JSON 自动拦截。
- 订单状态机：合法状态迁移、`client_order_id` 幂等防重。

## 2. 集成测试 (Integration Tests)
- Binance 与 OKX Demo/Testnet Adapter：交易规则同步、行情标准化、账户余额与持仓读取、模拟下单/撤单/查询与对账。
- 全链路闭环测试：Scanner -> Strategy Signal -> AI TradePlan -> Risk Engine -> Execution Engine -> Portfolio Reconciliation -> Journal。
- 回测引擎测试：含手续费、滑点、资金费率及样本内/样本外切分验证。

## 3. 故障注入测试 (Fault Injection Tests)
- 下单请求超时：`orders.status` 进入 `UNKNOWN`，禁止盲目重发，必须通过 `reconcile_unknown_order` 查询原单后恢复。
- AI 超时或返回非法 JSON：禁止新开仓并记录系统事件，整个交易流水线 Fail-Closed。
- 行情数据过期（`is_stale=True`）：风控拒绝新开仓。
- 本地与交易所持仓不一致：触发对账告警并冻结账户新开仓 (`sync_status=MISMATCH`)。
- 含提现权限的 API Key：系统阻断接入并标记 `REJECTED_WITHDRAW_PERMISSION`。

---

## 4. 已执行的自动化测试 (140 Passed)

在本地测试套件中，共计 **140 个自动化测试用例执行并通过**，分布在 10 个核心测试文件中：

| 测试模块 | 通过数 | 测试重点与覆盖内容 |
| :--- | :---: | :--- |
| `tests/test_ai_agents.py` | **9** | 验证 AI 结构化输出校验、TradePlan 必填字段、非法 JSON 阻断、AI 超时 Fail-Closed、敏感密钥彻底隔离与 Prompt 脱敏过滤、Token 消耗与成本追踪、5 大 Agent 运行状态。 |
| `tests/test_ai_provider.py` | **12** | 用 `httpx.MockTransport` 验证 OpenAI 兼容 Provider：JSON 契约、用量统计、Payload 脱敏、超时类型化、HTTP 错误不回显密钥、截断（`finish_reason=length`）识别、非 JSON 拒绝、未配置时拒调用；并覆盖系统强制过期策略（过去/离谱/缺失时间被覆盖且留审计）。 |
| `tests/test_core_and_schemas.py` | **5** | 验证价格与数量基于 `tick_size`/`lot_size` 的向下舍入、价差计算（bps）、合约乘数换算、本机密钥 Fernet 加密与掩码脱敏、TradePlanSchema 语义校验。 |
| `tests/test_execution_engine.py` | **16** | 验证订单状态机线性迁移、终态保护、非法跃迁拦截、`UNKNOWN` 一等状态处理、交易所原生止盈止损条件单、`client_order_id` 唯一性与幂等重放防重、成交与持仓记账、超时不盲目重发、风控拒绝不下发、Kill Switch 熔断、Reduce-Only 平仓与 Flatten All 紧急清仓。 |
| `tests/test_portfolio_accounting.py` | **6** | 验证组合账务不变量：手续费不被重复扣减、入金/出金/内部划转不计入交易利润、现货估值与合约保证金分别计量、本地与交易所持仓对账、数据新鲜度诚实降级（未知即显示未知）。 |
| `tests/test_postgres_dialect.py` | **9** | 在无服务器环境下用 PostgreSQL 方言编译全部建表语句：`DecimalNumeric` 映射为 `NUMERIC(28,10)`、`UTCDateTime` 强制 UTC、24 张表名与唯一约束齐全、`create_mock_engine` 可完整建表。 |
| `tests/test_rate_limit.py` | **10** | 验证加权令牌桶：端点权重、非阻塞拒绝、阻塞等待与超时、超容量立即拒绝、未知市场回退、快照完整性、8 线程并发安全，以及「适配器单次逻辑调用只计一次权重」的重入保护。 |
| `tests/test_risk_engine.py` | **13** | 覆盖 10 大硬性风控规则：行情过期/缺失拦截 (Rule 1)、TradePlan 过期拦截 (Rule 2)、账户未对账/锁定拦截 (Rule 3)、未处理 UNKNOWN 订单拦截 (Rule 4)、含提现权限 Key / Live 关闭拦截 (Rule 5)、价差过大拦截 (Rule 6)、止损方向错误/零距离拦截 (Rule 7)、日亏损/交易次数超限/Kill Switch 拦截 (Rule 8)、最大杠杆与总敞口超限 (Rule 9)、步长精度与最小名义价值拦截 (Rule 10)；包含精确口令复位恢复与异常 Fail-Closed 机制。 |
| `tests/test_risk_operator_reset.py` | **5** | 验证 Rule 8 锁定的人工复位通道：必须 `CONFIRM_RESET`、复位后解锁、写入审计与风险事件、**复位不删除任何成交历史**（append-only 水位线）、复位后计数器归零。 |
| `tests/test_system_and_recovery.py` | **55** | 验证健康检查探针 (`/health/live`, `/health/ready`)、Prometheus Metrics 格式、16GB 内存预算与压力评估、开机状态恢复全流程、未确认保护单锁定新开仓、恢复时不自动强平持仓、操作者确认解锁、无密钥泄露加密备份与恢复演练、全套 39 个 REST 端点 200 冒烟测试、8 个 WebSocket 频道注册、端到端自动化交易流水线（`run_automation_cycle`）。 |
| **总计** | **140** | **全部 Passed** |

---

## 5. PostgreSQL 实库集成验证 (已执行)

`make pg-check` (`infra/scripts/pg_integration_check.py`) 会启动一个**嵌入式真实 PostgreSQL**
（无需 Docker 或系统安装），在其上从头跑完所有 Alembic 迁移并验证数据库引擎相关内容：

| 检查项 | 结果 |
| :--- | :--- |
| `alembic upgrade head` 建表 | ✅ 24 张业务表 + `alembic_version` |
| 金额列类型 | ✅ `orders.qty / price / avg_fill_price` 均为 `NUMERIC(28, 10)` |
| 精度往返 | ✅ `NUMERIC(28,10)` 读回为 `Decimal`，不经过 float |
| JSON 列 | ✅ 策略参数、指标快照、审计快照均正常读写 |
| 唯一约束 | ✅ 重复 `client_order_id` 被数据库拒绝（`IntegrityError`） |
| 全链路流水线 | ✅ 连跑 2 轮，均 `blocked=None`、订单 `FILLED`，成交手续费保存为 `Decimal` |
| 迁移回滚/重跑 | ✅ `downgrade base` → 1 表；`upgrade head` → 25 表 |

> 该脚本也验证了 `DecimalNumeric` 在 PostgreSQL 上映射为真 `NUMERIC`，在 SQLite 上退化为字符串存储但仍返回 `Decimal`，两条路径都无浮点误差。

## 6. 尚未执行的测试 (Needs real credentials / hardware)

以下测试项目因需要真实物理硬件、真实外部网络环境或真实交易所 API 凭据，当前阶段明确标记为**未执行**，需在用户满足前提条件后进行实机演练：

1. **真实公网 Testnet/Demo API 长期集成测试**：
   - 依赖项：用户申请的真实 Binance Testnet 及 OKX Demo 模式 API Key/Secret。
   - 内容：真实公网 WebSocket 订阅长连接、私有账户成交回报推送延迟测试、远端交易所真实限频（Rate Limit）退避重试。
   - 现状：系统内置的适配器是**确定性内存模拟器**；限频治理（加权令牌桶）与 WebSocket 重连已实现并有单元测试，但尚未对真实交易所端点做过长连接压测。
2. **Mac mini 7–14 天常驻浸泡测试 (Soak Test)**：
   - 依赖项：目标物理硬件 Apple Silicon Mac mini 16GB。
   - 内容：在 launchd 常驻守护下连续运行 7 至 14 天，观测长时间运行下的内存泄漏（Memory Leak）、Swap 内存换入换出压力、Python GC 表现、日志轮转与磁盘占用增长。
   - 现状：日志已改为 `RotatingFileHandler`（10 MiB × 5，上限 50 MiB/logger），但长时间运行数据尚未采集。
3. **Mac mini 物理断电与 UPS 切换演练**：
   - 依赖项：实机配套 UPS、局域网路由器供电系统。
   - 内容：实测突然切断市电时 UPS 的供电接管、低电量下的安全优雅关机、市电恢复后 Mac mini 自动通电开机（"断电后自动启动"选项）以及 FileVault 磁盘解锁后的恢复行为。
4. **小额实盘 (Live) 首次发单与对账演练**：
   - 依赖项：具备真实交易资格的交易所账户、关闭提现权限的 API Key、操作者签署风险知情确认。
   - 内容：使用不超过 `LIVE_MAX_ORDER_NOTIONAL_USDT=100.00` 的极小金额进行实盘首笔下单、成交回报、原生止盈止损单挂载及全量对账验证。
5. **真实 LLM 端到端长跑**：
   - 依赖项：稳定的 OpenAI 兼容端点与有效 Key（`AI_API_KEY`）。
   - 现状：单次生成已实测通过（`gemini-3.8-flash`，约 11–12 秒、≈$0.0039/次策略调用），但未做长时间/高并发压测，也未评估供应商限流下的表现。
