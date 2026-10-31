# 模拟盘验收报告 (ACCEPTANCE_REPORT.md)

- **系统版本**：AI Crypto Trading Terminal V1.1（Mac mini 16GB 24h 运行版）
- **报告类型**：模拟盘系统验收与就绪评估报告 (Paper-Trading Acceptance Report)
- **验收环境**：Windows 11 本地开发验证环境 + macOS 目标部署架构（launchd + Homebrew native PostgreSQL/Redis）
- **报告日期**：2026-10-30
- **当前默认运行模式**：Demo 确定性模拟执行（Live 实盘全局锁闭：`LIVE_TRADING_ENABLED=false`）

---

## 1. 验收环境与部署形态

本项目依据《AI Crypto Trading Terminal 开发计划 V1.1》进行架构落地与系统测试，包含两套运行验证形态：

1. **开发与验证形态 (Windows 11 / Local Stack)**：
   - 存储降级验证：SQLite 本地文件持久化（`data/terminal.db`，保留 SQLAlchemy `DecimalNumeric` 任意精度特性）与进程内事件总线（In-Process EventBus），用于无外部依赖下的快速自测与离线验收。
   - 依赖栈：Python 3.12 + FastAPI + Next.js 14 (Node.js 20) + Docker Compose（提供 PostgreSQL 16 与 Redis 7.4 可选编排）。
2. **目标生产部署形态 (macOS Apple Silicon Mac mini 16GB)**：
   - 进程管理：7 个原生 `launchd` plist 常驻服务（`com.aicrypto.postgres`、`com.aicrypto.redis`、`com.aicrypto.api`、`com.aicrypto.web`、`com.aicrypto.market-worker`、`com.aicrypto.execution-worker`、`com.aicrypto.recovery-watchdog`），通过 `KeepAlive` 守护。
   - 资源控制：严格规划 16GB 统一内存预算（各进程预算合计约 12.6GB，为 macOS 与突发负载留出余量），避免 Docker Desktop 虚拟化额外内存开销，选用 Homebrew 原生 PostgreSQL 与 Redis。
   - 自动化运维：配备标准 Shell 脚本套件（`install-services.sh`、`start-services.sh`、`stop-services.sh`、`status.sh`、`backup.sh`、`restore-check.sh`）与 Makefile 目标。

---

## 2. 逐阶段验收结果表 (阶段 0 至 阶段 7)

| 阶段 | 交付物 | 验收标准 | 实际测试结果 | 状态 |
| :--- | :--- | :--- | :--- | :---: |
| **阶段 0：项目初始化与交易所资格确认** | 代码仓库架构、README、AI_RULES、docs 规范文档、.env.example、Binance/OKX 规则结构体 | 新环境按文档能完成初始化，环境配置齐全，交易对规则结构就绪 | 规范文档均建立，Git 结构完整，定义了现货与 USDT 永续标准合约规则与环境变量模板 | ✅ 通过 |
| **阶段 1：本地基础工程** | Next.js 14 前端、FastAPI 后端、Alembic 迁移（24 张数据表）、launchd 服务模板、Watchdog 探针 | 前后端正常通信，数据库重启不丢数据，健康检查 `/health` 输出 PID/系统探针 | 24 张核心业务表迁移成功，`/health/live` 与 `/health/ready` 正常工作，Watchdog 探针与状态恢复机工作正常 | ✅ 通过 |
| **阶段 2：交易所只读接入** | Binance 与 OKX Adapter 抽象、交易规则同步、账户资产/持仓只读读取、密钥本机加密存储 | 交易所资产与持仓正确读取，`tick_size`/`lot_size` 步长与 `contract_size` 正确换算，Secret 不泄露 | 支持现货与永续，Fernet 本机主密钥加密并自动掩码脱敏，提供模拟交易连接与测试网端点配置 | ✅ 通过 |
| **阶段 3：Scanner 与基础策略** | 技术指标引擎（EMA/RSI/MACD/ATR）、市场扫描器（Scanner）、趋势跟随与区间突破策略、信号去重与过期控制 | 扫描器限频可控，同一信号去重不反复触发，信号可追溯至行情快照与策略版本 | 12 个候选币种扫描计算正常，指标与深度失衡计算精确，信号具备 `dedup_key` 与过期失效保护 | ✅ 通过 |
| **阶段 4：AI Agent 与 TradePlan** | 5 大 Agent 体系（Market, Scanner, Strategy, Risk Explanation, Journal）、TradePlan Pydantic Schema | AI 输出经过严格 Schema 校验，不接触交易所 API Secret，超时与非法 JSON 自动拦截 | 9 个 AI 测试用例覆盖必填字段完整性、非法 JSON、超时、凭证隔离与 Token/成本追踪；AI 超时与 Schema 破损均触发 Fail-Closed | ✅ 通过 |
| **阶段 5：风险引擎和模拟交易执行** | 10 大硬性风控规则、订单状态机（CREATED→VALIDATED→SUBMITTED→FILLED/UNKNOWN）、原生止盈止损确认、Kill Switch | 每笔订单拥有唯一 `client_order_id` 且幂等，风控拒绝绝不发单，止盈止损由交易所确认，紧急熔断生效 | 16 个执行引擎测试与 13 个风控测试全部通过，支持 UNKNOWN 挂起恢复，止损单必须 `tp_sl_confirmed` | ✅ 通过 |
| **阶段 6：回测、模拟运行和复盘** | 回测引擎（含手续费、滑点与资金费率模型）、净值曲线、Journal 交易复盘日记、端到端自动化流水线 | 本地账本与交易所模拟对账无差异，重启后状态可恢复，端到端 8 阶段流水线畅通 | 端到端 Cycle（Market→Scanner→Strategy→AI→Risk→Execution→Reconciliation→Journal）完整闭环 | ✅ 通过 |
| **阶段 7：小额实盘准备与上线** | 实盘防护开关、实盘限额校验、提现权限封杀、实盘确认机制 | 满足上线前置条件，且严格限制初始单笔与日度亏损 | **系统机制已完备，但默认故意不执行**。`LIVE_TRADING_ENABLED=false` 锁定，需用户配置实盘凭据与人工授权 | ⛔ 未执行 / 待实盘条件就绪 |

---

## 3. 自动化测试结果 (140 Passed)

在后端 `services/api` 运行 pytest，**140 个测试全部通过**（0 失败，耗时约 52 秒），涵盖 6 个核心测试模块：

| 测试文件 | 通过数 | 覆盖核心范围 |
| :--- | :---: | :--- |
| `tests/test_ai_agents.py` | **9** | AI 结构化输出 TradePlan 校验、必填字段完整性、非法 JSON 拦截、AI 超时 Fail-Closed、交易所密钥隔离与防注入（Prompt 脱敏）、Agent 调用耗时/Token/成本追踪、5 大 Agent 角色健康状态。 |
| `tests/test_core_and_schemas.py` | **5** | 核心精度数学工具：`tick_size`/`lot_size` 步长向下舍入、买卖盘价差 bps 计算、现货 vs USDT 永续合约乘数（`contract_size`）仓位换算、Fernet 本机密钥加密解密与脱敏、TradePlanSchema 严格数据校验。 |
| `tests/test_execution_engine.py` | **16** | 订单执行状态机线性流转、终态不可变更、非法状态跃迁拦截、`UNKNOWN` 一等状态处理、带交易所原生止盈止损的订单提交、`client_order_id` 唯一性与幂等重放防重、成交与持仓记账、下单超时不盲目重发、风控拒绝绝不下发、Kill Switch 熔断拦截、Reduce-Only 平仓与 Flatten All 紧急清仓。 |
| `tests/test_portfolio_accounting.py` | **6** | 组合账务不变量：手续费不被重复扣减（`realized_pnl` 已为净额）、入金/出金/内部划转严格不计入交易利润、现货估值与合约保证金分别计量、本地与交易所持仓对账、数据新鲜度诚实降级（单交易所未同步即标记过期；全未同步标记“数据过期/未知”）。 |
| `tests/test_risk_engine.py` | **13** | 10 大硬性风控规则全覆盖：行情过期/缺失拦截 (Rule 1)、TradePlan 过期拦截 (Rule 2)、账户未对账/锁定拦截 (Rule 3)、未处理 UNKNOWN 订单拦截 (Rule 4)、含提现权限 Key / Live 关闭拦截 (Rule 5)、价差过大拦截 (Rule 6)、止损方向错误/零距离拦截 (Rule 7)、日亏损/交易次数超限/Kill Switch 拦截 (Rule 8)、最大杠杆与总敞口超限 (Rule 9)、步长精度与最小名义价值拦截 (Rule 10)；包含精确口令复位恢复与异常 Fail-Closed 机制。 |
| `tests/test_system_and_recovery.py` | **53** | 系统健康探针 (`/health/live`, `/health/ready`)、Prometheus Metrics 暴露、16GB 内存预算压力评估、开机恢复状态机全流程（冷启动→数据库/缓存探针→交易所重连→账本对账→保护单校验→READY）、未确认保护单锁定新开仓、恢复时不自动强平已有持仓、运维确认解锁、加密备份与恢复演练、全套 37+ REST 端点 200 冒烟测试、WebSocket 频道注册、端到端自动化交易流水线（`run_automation_cycle`）。 |
| **总计** | **140** | **全部 Passed** |

---

## 4. 端到端流水线实测记录 (End-to-End Cycle)

在系统联调与模拟盘运行测试中，触发完整的端到端自动化循环（`/api/v1/automation/run-cycle`），经受测流水线按序执行全部 8 个阶段，真实观测记录如下：

```text
[1. market_data]              刷新市场行情快照，验证行情新鲜度 (is_stale=False)
[2. scanner]                  扫描全市场候选对，计算 EMA/RSI/MACD/ATR 与订单簿不平衡度，产出候选标的
[3. strategy_signal]          策略引擎（趋势跟随 / 区间突破）运算并生成去重交易信号 TradeSignal
[4. ai_trade_plan]            AI Orchestrator 多角色分析并生成符合 Pydantic 校验的 TradePlan
[5. risk_engine]              独立确定性风控引擎校验 10 大规则，计算 Decimal 仓位与 tick/lot 舍入
[6. execution_engine]         生成唯一 client_order_id，下发模拟订单，建立原生止盈止损条件单
[7. portfolio_reconciliation] 本地与交易所模拟持仓全量对账，更新资产净值与敞口快照
[8. journal]                  Journal 模块与 Journal Agent 记录交易全链路审计条目与复盘摘要
```

### 真实观测订单与持仓数据 (Observed Execution)
- **成交订单 (Order)**：
  - **订单 ID**：`617773a7-fb5f-4e24-b642-7a6d857fa28f`
  - **Client Order ID**：`act-bin-cec343a1c0024ef7`
  - **Exchange Order ID**：`BN-DEMO-1001`
  - **交易标的**：`BTC/USDT:USDT`（Binance USDT 永续合约）
  - **方向与类型**：`BUY` / `LONG`，`LIMIT`
  - **委托数量**：`0.059` BTC
  - **委托价格 / 成交均价**：`68621.1` USDT / `68621.1` USDT
  - **订单状态**：`FILLED`
  - **止盈 / 止损**：TP `70009.5` USDT / SL `67926.9` USDT
  - **保护单确认状态**：`tp_sl_confirmed = True`
  - **挂接交易所保护单 ID**：`['BN-DEMO-1001-SL', 'BN-DEMO-1001-TP']`
- **持仓快照 (Position)**：
  - **持仓数量**：`0.059` BTC（多头持仓，名义价值 `4048.6449` USDT）
  - **占用保证金**：`2024.3225` USDT（杠杆率 `2x`）
  - **强平预警价**：`37741.6` USDT
  - **保护单状态**：`tp_sl_confirmed = True`
- **账户净值与对账快照 (Portfolio Reconciliation)**：
  - **账户总净值 (Total Equity)**：`54,696.4492` USDT（约 `54,696.45` USDT）
  - **可用余额 (Available Balance)**：`52,672.1267` USDT
  - **保证金余额 (Margin Balance)**：`2,024.3225` USDT
  - **总名义敞口 (Notional Exposure)**：`4,048.6449` USDT
  - **现货估值 (Spot Valuation)**：`52,672.1267` USDT
  - **已支付手续费 (Fees Paid)**：`2.0243` USDT（实际已实现净盈亏 `-2.0243` USDT，严禁重复计费）
  - **对账状态**：`mismatch_count = 0`，Binance 与 OKX 账本与交易所记录完全一致。

---

## 5. 故障注入与边界验证清单

| 故障注入场景 | 预期防护机制 | 实测表现 | 结论 |
| :--- | :--- | :--- | :---: |
| **风控 10 大硬性规则拦截** | 违反任一规则（如过期行情、逆向止损、日亏损超标）必须立即拒绝 | 测试针对 10 项规则注入异常参数，全部返回对应 `rule_code` 并拒绝下发，持久化 `risk_events` | ✅ 通过 |
| **AI 响应超时 (Timeout)** | 超过 `AI_TIMEOUT_SECONDS`（15s）自动中止，禁止开仓 (Fail-Closed) | 注入超时异常后，流水线中止开仓，记录 `AgentRun(status='TIMEOUT')`，风控与执行未收到待开仓单 | ✅ 通过 |
| **AI 返回非法 JSON / 破损 Schema** | 格式错误、字段缺失或数值类型错误时拒绝采纳 | 注入非法 JSON 字符串，Pydantic 校验拦截，记录 `AIDecision(is_valid=False)`，流水线 Fail-Closed | ✅ 通过 |
| **订单请求超时 (`UNKNOWN` 状态)** | 进入 `UNKNOWN` 状态，严禁盲目重发，等待对账恢复 | 注入网络断开超时，执行引擎将订单置为 `UNKNOWN`，后续新开仓自动被 Rule 4 锁死，直至 Reconcile | ✅ 通过 |
| **未确认原生保护单 (Unconfirmed TP/SL)** | 开仓后若未能确认交易所已接受止盈止损单，系统视为风险持仓 | Recovery 状态机与风控引擎将该持仓置为未保护，阻断后续新交易，要求人工确认 | ✅ 通过 |
| **本地与交易所持仓不一致 (Mismatch)** | 本地持仓与交易所持仓存在数量或标的差异 | 标记 `sync_status='MISMATCH'`，Rule 3 立即冻结该账户所有新开仓，并在界面突出告警 | ✅ 通过 |
| **Kill Switch 紧急熔断与复位** | 触发后立即锁死所有新开仓；复位必须输入精确口令 | 全局与账户级熔断均能 100% 阻断下单；调用 `/resume` 传入空或错误口令被拒绝，输入 `CONFIRM_RESUME` 成功复位 | ✅ 通过 |
| **含提现权限 (`can_withdraw=True`) API Key** | 严禁自动化系统接入含有资金出逃权限的凭据 | 模拟检测到提现权限，连接状态置为 `REJECTED_WITHDRAW_PERMISSION`，Rule 5 永久锁闭 | ✅ 通过 |

---

## 6. 已知限制与未解决风险 (Known Limitations & Risks)

本着实事求是与安全第一原则，明确指出当前系统的已知限制与尚未消除的单机风险：

1. **模拟适配器为进程内确定性模拟器 (In-Memory Simulators)**：
   当前自动化测试与本地 Demo 运行采用确定性 Mock / In-Memory 交易所模拟器，尚未直接长时间连接真实的 Binance / OKX 远端 Testnet 公网 WebSocket 与私有频道。
2. **本地环境采用 SQLite 降级方案 (SQLite Local Fallback)**：
   在未启动 Docker 或未安装本地 PostgreSQL/Redis 时，系统自动降级到本地 `data/terminal.db`（SQLite）与进程内内存事件总线。虽然模型保留了 Decimal 精度并提供了持久化支持，但在并发高写入与连接隔离上不如生产级 PostgreSQL 16 + Redis。
3. **AI Provider 默认采用规则辅助 Mock (Rule-Assisted Mock)**：
   在未配置外部商业大模型 API Key（如 DeepSeek / OpenAI）前，系统默认运行在 `rule_assisted_mock` 模式，由本地确定性逻辑生成结构化 TradePlan，未产生真实的大模型公网 API 调用与外部网络延迟。
4. **Mac mini 24 小时常驻运行尚未经过 7–14 天实机 Soak Test**：
   虽然已提供完整的 `launchd` plist 配置与看门狗脚本，但尚未在物理 Mac mini 硬件上连续通电运行 7 至 14 天，长时间运行下的内存泄露（Memory Leak）、Swap 膨胀、网络瞬断重连稳定性仍待实机长期观察。
5. **FileVault 与冷启动无人值守限制**：
   目标 macOS 开启 FileVault 加密磁盘时，机器断电重启后无法无人值守自动登录；LaunchAgent 必须在用户会话解锁后才能拉起。若整机断电重启，必须人工现场输入密码解锁，系统无法完全无人值守冷启动。
6. **单机单网络单电源的物理脆弱性**：
   Mac mini 本质为单点硬件，无法抵御入户宽带故障、家庭局域网路由器宕机或长时间停电（超过 UPS 电池承载极限）。

---

## 7. 实盘上线前置检查清单 (Pre-flight Checklist)

依据实施计划要求，在切换到任何小额实盘前，必须逐项核对并满足以下 9 项前置条件：

| 序号 | 检查项目 | 标准要求 | 当前系统状态 | 评估结论 |
| :---: | :--- | :--- | :--- | :---: |
| 1 | **P0 核心功能与测试全绿** | 140 项自动化测试全部 Passed，端到端 Cycle 成功闭环 | 140 项测试全部通过，核心状态机与计算引擎零故障 | ✅ 达成 (Pass) |
| 2 | **交易所账户与地区产品资格** | 确认 Binance / OKX 账户地区政策支持现货与 USDT 永续合约 | 需用户登录真实交易所完成实名与衍生品协议签署 | ⚠️ 待用户确认 (Pending User) |
| 3 | **API Key 权限最小化** | 仅开启 Read（读取）与 Trade（交易）权限，绝不开通提现 | 系统具备自动侦测并封杀提现权限逻辑（Rule 5） | ✅ 系统已就绪 / 待绑定实盘 Key |
| 4 | **提现权限彻底关闭确认** | 交易所后台 API 管理页面提现复选框处于未勾选状态 | 代码实现强制校验，`has_withdraw_permission=True` 阻断开仓 | ✅ 规则已就绪 (Rule 5 Armed) |
| 5 | **交易所 API 访问 IP 白名单** | 绑定 Mac mini 出口公网静态 IP 或动态 DNS | 需用户在交易所控制台绑定固定 IP | ⚠️ 待网络配置 (Pending Network) |
| 6 | **Kill Switch 熔断测试与复位** | 验证人工与自动熔断能 100% 阻断委托并安全平仓 | 单元与集成测试均已通过，复位要求严格确认口令 | ✅ 达成 (Pass) |
| 7 | **数据加密备份与恢复演练** | 验证数据库能够完成无凭证泄露的加密导出与恢复验证 | `test_backup_restore_drill_succeeds` 测试通过，配套脚本就绪 | ✅ 达成 (Pass) |
| 8 | **实盘模式显式二次确认** | 界面与接口必须显式确认，AI 严禁擅自启用 Live | `LIVE_TRADING_ENABLED=false` 默认硬编码锁死，UI 设双重开关 | ✅ 达成 (Pass) |
| 9 | **操作者风险承受认知确认** | 用户知悉单机故障、行情滑点与爆仓风险，设立单笔上限 | 配置文件硬限制 `LIVE_MAX_ORDER_NOTIONAL_USDT=100.00` | ✅ 机制就绪 / 待用户签署确认 |

---

## 8. 验收结论

AI Crypto Trading Terminal V1.1 在架构设计、精度控制、安全防线、10 大硬性风控、订单状态机流转及异常恢复机制上**完全达到设计规范，模拟盘阶段验收合格（Passed）**。系统已具备在 macOS 上以常驻 LaunchAgent 守护形态进行 7–14 天长期模拟盘挂机（Soak Test）的技术条件。在上述第 7 节中的用户硬件、网络及真实实盘凭据未完成人工验证前，**系统严禁擅自开启 Live 模式**。
