# AI Crypto Trading Terminal (V1.1 — Mac mini 16GB 24h Edition)

个人版 AI 加密货币交易终端 —— 运行在本机的浏览器 Web 终端（`http://localhost:3000`），统一接入 **Binance** 与 **OKX**（现货 + USDT 本位永续合约），包含实时行情与市场扫描器（Market Scanner）、可回测规则策略引擎、多角色 AI Agent 结构化交易计划生成、确定性独立风控引擎（Risk Engine）、幂等订单执行状态机（Execution Engine）、账户对账与交易复盘（Journal & Backtest）。

## 核心架构原则

1. **职责边界清晰**：AI 负责分析候选机会与输出结构化 `TradePlan`；确定性规则（`RiskEngine`）负责风控审核与 Decimal 仓位计算；`ExecutionEngine` 负责与交易所通信与订单状态机管理；**交易所实际订单与成交记录是账户状态的最终依据**。
2. **安全第一（Fail-Closed）**：
   - API Secret / Passphrase 使用本机主密钥（优先系统 Keychain / Credential Store）加密存储，数据库仅保存密文和掩码，绝不暴露给前端、日志或 LLM Prompt。
   - 若检测到 API Key 含有提现（`withdraw`）权限，拒绝用于自动交易。
   - 行情过期、AI 超时/格式错误、对账不一致或风控异常时，一律 **Fail-Closed（禁止新开仓）**。
   - 默认运行在 **Demo / Testnet 模拟环境**，实盘（Live）默认关闭且必须经过显式二次确认与严格限额校验。
3. **精度与幂等性**：
   - 所有资金、价格、数量、保证金与手续费均使用 `Decimal` / 数据库 `NUMERIC` 计算，严禁使用浮点数。
   - 每笔订单生成唯一 `client_order_id`，下单超时进入 `UNKNOWN` 一等状态，必须先向交易所查询与对账，严禁盲目重试。
4. **数据诚实性**：交易所数据不可用时显示「数据过期/未知」，**绝不用旧值或假数据伪装实时行情**。

## 当前实现状态 (V1.1)

- **后端架构**：FastAPI + SQLAlchemy 2.0 + Alembic，24 张核心业务表，全模块已实现 —— Market Data、Scanner、Strategy、AI Orchestrator、Risk Engine、Execution Engine、Portfolio Reconciliation、Journal & Backtest、Background Workers。
- **前端暗色终端**：Next.js 14 App Router + Tailwind CSS，8 大导航板块（Overview 总览、Exchanges 交易所连接、Markets 市场与行情、AI System 智能体控制台、Trading 交易执行、Risk Center 风控中心、Analytics 绩效与回测、Settings 系统与运维）。
- **自动化测试**：**140 个 pytest 测试全部 Passed**，覆盖精度计算、10 大硬性风控规则、订单状态机与幂等、AI 结构化输出与超时拦截、AI Provider 适配器（JSON 契约/超时/截断/密钥隔离/过期策略）、组合账务不变量、冷启动恢复状态机、备份恢复演练及 REST/WebSocket 冒烟测试。
- **AI Provider**：支持任意 OpenAI 兼容 `/v1/chat/completions` 端点（`AI_PROVIDER=openai_compatible`），或使用确定性规则模板离线运行（`rule_assisted_mock`，默认，亦为测试模式）。Provider 实测成功时单次 TradePlan 生成约 11-12 秒、约 2400 tokens、成本 ≈ $0.0035。设置页可直接「测试连接」。
- **常驻部署与运维**：面向 macOS Apple Silicon Mac mini 16GB，提供 7 个原生 `launchd` plist 守护配置与运维脚本（`make install-services` / `start` / `stop` / `status` / `backup` / `restore-check` / `reconcile` / `kill-switch`），避免 Docker Desktop 虚拟化层常驻开销。
- **明确安全说明**：系统**默认以 Demo 确定性模拟执行模式运行**，实盘开关全局锁定（`LIVE_TRADING_ENABLED=false`），任何实盘开仓均需操作者在硬件、网络与权限均就绪后显式人工确认。
- **已知限制**：内置交易所适配器为确定性模拟器（非真实 Binance/OKX Testnet 连接）；PostgreSQL 已用嵌入式实例实测通过（`make pg-check`），无 Docker 时降级为本地 SQLite + 进程内事件总线；Mac mini 7–14 天连续运行（Soak Test）尚待实机验证。

## 验收入口

端到端流水线实测记录、逐阶段交付验收清单、自动化测试拆解与故障注入实测，请查阅完整验收报告：

👉 **[模拟盘验收报告 (docs/ACCEPTANCE_REPORT.md)](docs/ACCEPTANCE_REPORT.md)**

## 目录结构

```text
.
├── apps/
│   └── web/                      # Next.js + TypeScript + Tailwind 暗色交易终端
│       ├── app/                  # App Router（page.tsx / layout.tsx / globals.css）
│       ├── components/           # UI 原子组件、SVG 图表与 8 大板块 sections
│       ├── lib/                  # 类型安全的 API 客户端（绝不伪造数据）
│       └── tests/
├── services/
│   └── api/                      # Python + FastAPI + SQLAlchemy 2 + Alembic 后端
│       ├── app/
│       │   ├── api/v1/           # REST 路由 (/api/v1) 与 8 通道 WebSocket (/ws/*)
│       │   ├── core/             # 配置、密钥加密、结构化日志、精度工具、恢复状态机、Watchdog
│       │   ├── db/               # 引擎、会话、Decimal 类型、自动迁移引导
│       │   ├── models/           # 24 张核心数据库模型实体
│       │   ├── schemas/          # Pydantic 严格数据与 TradePlan 校验模型
│       │   ├── exchanges/        # ExchangeAdapter 协议及 Binance / OKX 实现
│       │   ├── market_data/      # 行情标准化与技术指标引擎 (EMA/RSI/MACD/ATR)
│       │   ├── scanner/          # 市场扫描与机会排序
│       │   ├── strategies/       # 趋势跟随与区间突破策略及不可变版本控制
│       │   ├── ai/               # Market / Scanner / Strategy / Risk / Journal Agents
│       │   ├── risk/             # 独立确定性风控引擎与 Kill Switch
│       │   ├── execution/        # 订单状态机、幂等执行、UNKNOWN 恢复
│       │   ├── portfolio/        # 持仓、余额、净值快照与对账服务
│       │   ├── journal/          # 交易复盘、绩效统计与历史回测引擎
│       │   └── workers/          # 后台任务调度与 launchd Worker 入口
│       ├── alembic/              # 数据库 Schema 迁移脚本
│       └── tests/                # 单元、集成与故障注入测试 (140 passed)
├── packages/
│   └── shared-contracts/         # 前后端共享 TypeScript 类型定义
├── infra/
│   ├── launchd/                  # 7 个 macOS launchd 服务模板（postgres/redis/api/web/workers/watchdog）
│   ├── scripts/                  # dev / install-services / start / stop / status / backup / restore-check
│   └── docker/                   # Docker 辅助配置（仅开发与集成测试）
├── docs/                         # PRD / 架构 / 数据库 / API / 安全 / 测试 / 运维手册 / 验收报告
├── data/                         # 本地持久化数据与加密备份目录
├── logs/                         # 本地结构化运行日志
├── .env.example                  # 环境变量模板
├── docker-compose.yml            # PostgreSQL + Redis（仅绑定 127.0.0.1，仅开发/测试）
├── AI_RULES.md                   # AI 编程与协作铁律
└── Makefile                      # 常用开发与运维命令
```

## 快速开始

### 1. 准备环境变量

```bash
cp .env.example .env
```

### 2. 启动基础服务（可选）

若已安装 Docker Desktop / OrbStack（**仅开发与集成测试需要**）：

```bash
docker compose up -d postgres redis
```

若未安装 Docker，系统默认使用 `data/terminal.db`（保留 `Decimal` 精度的本地持久化数据库）和进程内事件总线，无需额外配置即可完整运行。

### 3. 安装依赖并启动后端 API

```bash
cd services/api
pip install -e ".[dev]"
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

> 空数据库首次启动会自动执行 Alembic 迁移并灌入 Demo 交易所连接、交易对、K 线与默认策略，无需手工建表。

### 4. 启动前端 Web 终端

```bash
cd apps/web
npm install
npm run dev
```

### 5. 访问地址

- **Web 交易终端**：`http://localhost:3000`
- **API 交互文档 (Swagger)**：`http://127.0.0.1:8000/docs`
- **健康检查**：`http://127.0.0.1:8000/health`（另有 `/health/live`、`/health/ready`、`/metrics`）

### 6. 运行自动化测试

```bash
cd services/api
pytest -v
```

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `make dev` | 启动完整本地开发栈（脚本自动编排 API + Web） |
| `make infra-up` / `make infra-down` | 启动 / 停止 Docker PostgreSQL + Redis（仅开发测试） |
| `make migrate` | 执行 Alembic 数据库迁移 |
| `make api` / `make web` | 单独启动后端 / 前端开发服务器 |
| `make test` | 运行后端 Pytest 全量测试 |
| `make install-services` | 安装 / 更新 7 个 macOS `launchd` 常驻服务配置 |
| `make start` / `make stop` | 启动 / 停止常驻服务（`stop` **不会**自动撤单或平仓） |
| `make status` | 查看进程、依赖与交易所连接状态 |
| `make backup` / `make restore-check` | 执行加密备份 / 在隔离读取器验证备份可恢复 |
| `make reconcile` | 手动执行交易所对账 |
| `make kill-switch` | 禁止新开仓（紧急熔断） |

## 运行模式说明

| 模式 | 说明 |
| --- | --- |
| **Demo / Testnet（默认）** | 确定性模拟撮合，用于策略验证与演练；订单状态机、风控、对账与复盘全链路真实运行 |
| **Live（默认锁定）** | `LIVE_TRADING_ENABLED=false`。需后端显式开启、通过 9 项上线前置检查，并在界面输入确认短语后方可启动；单笔名义价值受 `LIVE_MAX_ORDER_NOTIONAL_USDT` 限制 |

> 本系统不承诺收益，也不保证损失上限。回测与模拟盘结果不代表未来表现。
