# AI Crypto Trading Terminal

## 个人版开发计划与实施说明（Markdown）

-   文档版本：V1.1（Mac mini 16GB 24小时运行版）
-   产品形态：浏览器网页端（本机访问）
-   运行环境：macOS
-   开发方式：以 AI 编程工具辅助开发，人工审查与测试
-   交易所：Binance、OKX
-   市场范围：现货 + USDT 本位永续合约（按账户地区和交易所资格启用）
-   自动化模式：AI 自动分析与生成交易计划，独立风控引擎审核后执行
-   建议首版周期：约 8--12 周；取决于开发者投入、AI
    编程工具质量、交易所资格和测试深度

------------------------------------------------------------------------

# 1. 项目概述

## 1.1 产品目标

开发一套运行在 Mac 上的个人 AI
加密货币交易终端。用户在浏览器中打开本机网页，例如
`http://localhost:3000`，即可查看账户、行情、策略、AI Agent
状态、订单、持仓、风险和交易复盘。

第一版连接 Binance 和 OKX，统一现货与 USDT
本位永续合约的数据结构和交易接口。系统持续扫描市场，计算技术指标和市场特征，由
AI
模型分析候选机会并生成结构化交易计划。计划必须经过确定性的风险规则校验，才允许交易执行器向交易所提交订单。

**核心原则：AI
负责分析和提出计划；程序化规则负责风控；执行器负责交易所通信；交易所实际订单和成交记录是账户状态的最终依据。**

## 1.2 V1.0 目标

1.  在 Mac 本机启动前端、后端、数据库和后台 Worker。
2.  浏览器访问本地网页完成全部主要操作。
3.  接入 Binance 和 OKX 的只读账户与公共行情。
4.  统一展示现货资产、合约余额、持仓、订单和成交。
5.  支持现货与 USDT 本位永续合约的模拟交易。
6.  支持市场扫描、策略信号、AI 分析和交易计划。
7.  建立独立风险引擎、订单状态机、对账机制和紧急停止。
8.  提供回测、模拟盘统计、交易日志和审计记录。
9.  经过严格验收后，才允许用户主动开启小额实盘。

## 1.3 V1.0 不做的事情

-   不做 SaaS 多用户和公开注册。
-   不做社交跟单、策略市场和用户资金托管。
-   不做提币、转账、资金划转自动化。
-   不做现货杠杆、币本位合约、交割合约和期权。
-   不做毫秒级高频交易或做市。
-   不允许大语言模型直接持有交易所 API Secret 或绕过风控下单。
-   不承诺收益，不以某个回测收益作为上线依据。

## 1.4 重要前提

-   交易所产品可用性取决于账户注册地区、所在地法律法规、账户等级和交易所政策。开发前必须逐项核实
    Binance、OKX 对该账户的现货、永续合约和 API 权限。
-   部分地区或账户可能无法使用某些衍生品产品。软件必须按账户实际返回的产品和权限动态启用功能，不得通过伪造地区或规避限制使用服务。
-   目标硬件为 Apple Silicon Mac mini、16GB 统一内存。首版采用云端 AI
    模型
    API，本机运行行情采集、指标计算、风控、交易执行、数据库和网页端；不在本机运行大型
    LLM。Mac mini
    可以承担个人规模的持续运行，但它仍是单机，无法消除断电、网络中断、硬件故障和交易所故障。必须实现启动恢复、交易所对账、熔断和外部告警。
-   V1.1 的 24 小时部署优先采用 macOS 原生进程 + launchd；Docker Compose
    保留给开发、集成测试和可选部署。这样可减少 16GB
    机器上的容器虚拟化开销。若使用 Docker
    Desktop，必须明确它的启动依赖用户登录会话。

------------------------------------------------------------------------

# 2. 用户与使用场景

## 2.1 唯一用户角色

V1.0
只有一个本地管理员用户。暂不实现多租户、组织、邀请和角色权限系统，但数据库字段可预留
`owner_id`，避免未来迁移困难。

## 2.2 主要使用场景

1.  用户启动 Mac 上的项目服务，浏览器打开网页。
2.  用户添加交易所 API Key，系统检查权限并测试连接。
3.  用户查看交易所账户资产、现货余额和合约持仓。
4.  用户选择交易所、市场类型、交易对和策略。
5.  用户启动 Scanner，查看候选交易机会。
6.  AI 对候选机会进行分析并输出结构化计划。
7.  Risk Engine 校验交易计划和账户风险。
8.  模拟执行器在 Demo/Testnet 环境下创建、更新和关闭订单。
9.  用户查看交易日志、净值曲线、回撤和策略表现。
10. 在所有验收条件通过后，用户可以单独启用 Live
    环境，并设置严格风险上限。

------------------------------------------------------------------------

# 3. 产品功能需求（PRD）

优先级定义：

-   **P0**：V1.0 必须实现。
-   **P1**：V1.0 后半段实现或紧随其后。
-   **P2**：V2.0 或以后。

## 3.1 Dashboard 总览（P0）

展示：

-   总权益及计价货币。
-   Binance、OKX 分账户权益。
-   现货资产估值。
-   合约保证金、未实现盈亏。
-   已实现盈亏、未实现盈亏。
-   手续费和资金费率收支。
-   当日盈亏、累计盈亏。
-   当前持仓数量、未成交订单数量。
-   当前运行策略和 Agent 状态。
-   最近交易、风险告警、连接状态。
-   数据最后更新时间和数据新鲜度。

要求：

-   所有统计必须标注数据时间。
-   交易所数据不可用时显示"数据过期/未知"，不可用旧值伪装实时数据。
-   区分账户权益、可用余额、保证金余额和名义敞口。
-   入金、出金和内部转账不能计入交易利润。

## 3.2 交易所连接管理（P0）

支持：

-   Binance。
-   OKX。

功能：

-   新增、编辑、停用和删除本地连接。
-   选择 Demo/Testnet 或 Live 环境。
-   输入 API Key、Secret；OKX 还需 Passphrase。
-   连接测试、权限检查、账户产品能力检查。
-   显示连接状态、最近成功同步时间和错误原因。
-   密钥只显示掩码，不允许再次读取明文。
-   断开连接后停止依赖该账户的新增交易。

安全要求：

-   只申请 Read 和 Trade 权限；绝不申请 Withdraw。
-   若检测到提现权限，拒绝将该连接用于自动交易并给出警告。
-   尽可能设置交易所 IP 白名单；本地家庭网络若公网 IP
    动态变化，应明确提示白名单维护问题。
-   API Secret 不得出现在前端、浏览器 LocalStorage、日志、异常堆栈或 AI
    Prompt 中。
-   密钥使用本机密钥加密后保存；优先使用 macOS Keychain
    保存主密钥，数据库只保存密文。
-   首版仅允许从 `localhost/127.0.0.1`
    访问管理页面，不向公网暴露服务端口。

## 3.3 市场行情（P0）

支持：

-   交易对列表和产品状态。
-   最新成交价、买一卖一、24 小时成交量。
-   K 线：1m、5m、15m、1h、4h、1d（首版按实际策略选择）。
-   订单簿深度摘要。
-   合约标记价格、指数价格、资金费率和下一次资金费率时间（交易所提供时）。
-   交易规则：tick size、lot size、min quantity、min notional、contract
    size 等。
-   WebSocket 实时订阅与断线重连。
-   REST 补偿和历史数据回补。

数据规范：

-   内部使用统一 `canonical_symbol`，同时保留交易所原生 symbol。
-   明确区分 Last Price、Mark Price、Index Price。
-   所有时间戳统一 UTC。
-   K 线必须标识是否已收盘；策略默认只使用已收盘 K
    线，避免未收盘数据造成信号漂移。
-   对数据增加 `source_exchange`、`received_at`、`event_time` 和
    `is_stale`。

## 3.4 Market Scanner（P0）

扫描范围可配置：

-   指定主流币列表。
-   指定交易所全部合格 USDT 交易对。
-   现货和永续分别扫描。
-   按 24 小时成交额、价差、波动率、成交量过滤。

首版扫描特征：

-   价格变化率。
-   成交量相对均值变化。
-   ATR 和历史波动率。
-   RSI、MACD、EMA。
-   趋势方向和趋势强度。
-   买卖价差。
-   订单簿深度和不平衡度。
-   永续资金费率。
-   数据新鲜度和交易状态。

输出：

-   候选交易对。
-   触发原因。
-   指标快照。
-   时间周期。
-   扫描时间。
-   风险过滤结果。
-   是否进入 AI 分析队列。

Scanner 不应对每个交易对高频发起 REST 请求。优先使用交易所公共 WebSocket
和批量行情接口，遵守交易所限频规则。

## 3.5 Strategy Engine（P0）

首版策略以可解释、可回测的规则策略为主，AI
负责补充分析，而不是从零自由发明所有交易规则。

建议先实现两种基准策略：

1.  趋势跟随：EMA 趋势过滤 + ATR 风险止损 + 成交量过滤。
2.  突破策略：区间高低点突破 + 成交量确认 + 波动率过滤。

每个策略必须有：

-   唯一 ID 和版本号。
-   适用交易所和市场类型。
-   适用交易对和周期。
-   入场条件。
-   退出条件。
-   风险参数。
-   冷却时间。
-   最大持仓数。
-   参数配置和变更记录。
-   回测结果和模拟盘表现。

策略参数修改必须产生新版本，不得覆盖历史版本。

## 3.6 AI Agent（P0）

建议使用工作流/状态机管理，不采用自由聊天式 Agent 群。

Agent 分工：

-   Market Agent：总结市场状态和行情特征。
-   Scanner Agent：对候选机会排序和去重。
-   Strategy Agent：评估规则信号并生成交易计划。
-   Risk Agent：提供风险解释，但不能替代程序化风控。
-   Journal Agent：解释决策、生成复盘摘要。

AI 输入：

-   已验证的行情特征。
-   已收盘 K 线摘要。
-   指标值。
-   账户风险摘要。
-   当前持仓和未成交订单摘要。
-   策略版本和风险配置。

AI 不得获得：

-   API Key、Secret、Passphrase。
-   交易所签名材料。
-   数据库密码。
-   任意 Shell 或任意代码执行权限。

AI 输出必须经过 JSON Schema/Pydantic 校验，字段至少包括：

-   exchange
-   market_type
-   symbol
-   direction
-   entry_type
-   entry_price 或 entry_zone
-   stop_loss
-   take_profit
-   confidence
-   timeframe
-   thesis
-   invalidation_conditions
-   expires_at
-   strategy_version

AI 输出仅是
`TradePlan`，不是可直接执行的订单。风险引擎必须独立重新计算数量、名义价值、保证金、费用缓冲和潜在亏损。

## 3.7 Risk Engine（P0，最高优先级）

所有新增订单必须先通过风险引擎。AI 不得跳过或修改风险规则。

首版配置项：

-   每笔最大计划风险。
-   每日最大亏损。
-   最大总敞口。
-   最大并行持仓数。
-   每个交易对最大敞口。
-   最大杠杆。
-   最大滑点。
-   最大买卖价差。
-   最大行情数据延迟。
-   连续 API 失败次数。
-   连续交易亏损次数。
-   当日交易次数上限。
-   交易所/账户级暂停开关。

建议模拟阶段采用保守默认值，例如每笔风险预算不高于权益的
0.25%、每日亏损阈值 1%、合约杠杆上限 2x、最多 3
个并行持仓。它们只是初始测试参数，不是安全保证；上线前必须由用户明确配置和确认。

风控规则：

1.  数据过期或 WebSocket 断流时禁止新开仓。
2.  AI 无响应、输出格式错误或超时时禁止新开仓。
3.  账户余额或仓位同步失败时禁止新开仓。
4.  下单结果未知时先查询和对账，不得盲目重试。
5.  交易所产品不支持或账户不具备资格时禁止交易。
6.  价格精度、数量精度和最小名义价值不符合规则时拒绝订单。
7.  止损距离为零、方向错误或止损价格不合理时拒绝订单。
8.  达到日亏损阈值后锁定新开仓，直到用户明确复位。
9.  风控服务自身异常时采用 Fail Closed：拒绝新开仓。
10. 已有仓位的保护和对账不能依赖 AI 服务继续运行。

仓位计算基础：

`risk_budget = account_equity * risk_rate`

`raw_qty = risk_budget / abs(entry_price - stop_price)`

随后必须根据市场类型、合约面值、乘数、手续费、滑点、最小数量、数量步长和保证金规则进行精确换算。合约不能直接套用现货数量公式。

## 3.8 Execution Engine（P0）

订单生命周期：

`CREATED → VALIDATED → SUBMITTED → ACKNOWLEDGED → PARTIALLY_FILLED → FILLED`

其他状态：

`REJECTED / CANCELED / EXPIRED / UNKNOWN`

要求：

-   每个订单生成唯一 `client_order_id`。
-   数据库对账户和 client_order_id 建唯一约束。
-   支持限价、市价和交易所明确支持的条件单。
-   处理部分成交、撤单、拒单、超时和重复事件。
-   下单超时后先查询原订单状态，不得直接重新下单。
-   订单更新以交易所推送为主，定期 REST 对账为辅。
-   现货卖出前检查可用资产。
-   合约平仓使用交易所对应的 reduce-only/平仓语义。
-   止盈止损优先使用交易所原生保护订单；必须验证订单已被交易所接受。
-   本地监控只能作为补充，不能将本地定时任务误认为交易所原生止损。

## 3.9 Portfolio、Positions 和对账（P0）

-   定时拉取账户余额。
-   订阅账户、订单、成交和持仓推送。
-   定期对账本地订单与交易所订单。
-   定期对账本地持仓与交易所持仓。
-   记录手续费、资金费率、入金、出金和内部转账。
-   分开统计已实现和未实现盈亏。
-   识别本地状态与交易所状态不一致的情况。
-   不一致时禁止相关账户新开仓，直至对账完成。

## 3.10 Analytics 和 Journal（P1）

统计指标：

-   Equity Curve。
-   Realized PnL。
-   Unrealized PnL。
-   Fees。
-   Funding PnL。
-   Win Rate。
-   Profit Factor。
-   Max Drawdown。
-   Sharpe Ratio（说明采样周期和计算假设）。
-   Average Win/Loss。
-   Strategy Performance。
-   Exchange Performance。

Journal 每笔交易保存：

-   交易信号。
-   策略 ID 和版本。
-   AI Prompt/模型版本（不含密钥）。
-   AI 结构化输出。
-   风控检查结果。
-   下单请求摘要。
-   交易所订单 ID。
-   实际成交。
-   手续费和资金费率。
-   平仓原因。
-   实际净盈亏。
-   复盘摘要。

## 3.11 Backtest（P1）

-   使用历史 K 线和必要的费率数据。
-   计入手续费、滑点和资金费率。
-   处理未收盘 K 线，避免前视偏差。
-   区分训练、验证和样本外区间。
-   记录数据来源和缺失区间。
-   报告最大回撤、收益波动、交易次数、盈亏比和费用占比。
-   不得将回测结果描述为未来收益保证。

------------------------------------------------------------------------

# 4. 技术架构

## 4.1 总体架构

采用"模块化单体 + 独立后台 Worker"的架构，避免首版过早引入微服务。

``` text
Browser
  |
  | http://localhost:3000
  v
Next.js Frontend
  |
  | REST / WebSocket
  v
FastAPI Backend
  |
  +-- Auth & Settings
  +-- Exchange Connections
  +-- Market Data Service
  +-- Scanner
  +-- Strategy Engine
  +-- AI Orchestrator
  +-- Risk Engine
  +-- Execution Engine
  +-- Portfolio & Reconciliation
  +-- Analytics & Journal
  |
  +--------------------+
  |                    |
  v                    v
PostgreSQL           Redis
  ^
  |
Background Workers
  |
  +-- Market Worker
  +-- Strategy Worker
  +-- AI Worker
  +-- Execution Worker
  +-- Reconciliation Worker
  |
  +----------------------------+
  |                            |
  v                            v
Binance API / WebSocket     OKX V5 API / WebSocket
```

## 4.2 技术选型

  层级            技术                                    用途
  --------------- --------------------------------------- ---------------------------
  Web 前端        Next.js + React + TypeScript            浏览器交易终端
  UI              Tailwind CSS + shadcn/ui                暗色终端界面
  图表            TradingView Lightweight Charts          K 线和净值曲线
  前端数据        TanStack Query                          REST 数据缓存
  前端状态        Zustand（按需）                         页面状态
  后端            Python + FastAPI                        REST、WebSocket
  数据校验        Pydantic                                API 和 AI 输出校验
  ORM             SQLAlchemy 2                            数据库访问
  Migration       Alembic                                 数据库迁移
  数据库          PostgreSQL                              业务数据
  缓存/队列       Redis                                   缓存、事件和任务
  交易所 SDK      优先官方接口；可评估 CCXT               统一接入辅助
  AI 接入         可替换 Provider Adapter                 模型供应商解耦
  测试            Pytest、Vitest、Playwright              单元、集成、端到端
  部署/进程管理   launchd + Homebrew；Docker Compose      Mac 24小时运行 / 开发测试
  日志            structlog 或标准结构化日志              运行审计
  监控            本地健康检查；后续 Prometheus/Grafana   服务监控

注意：CCXT
可用于加速公共行情和部分通用交易接口开发，但涉及交易所特有的合约模式、条件订单、持仓模式和订单状态时，必须核对原生
API。不要因为 SDK 返回成功就跳过交易所对账。

## 4.3 Mac mini 16GB 本机运行拓扑

V1.1 将 Mac mini
作为个人常驻交易主机。推荐将长期运行的业务进程直接运行在 macOS 上，由
`launchd` 监督；PostgreSQL 和 Redis 优先使用原生服务。Docker Compose
用于开发、集成测试和临时验证，不作为 24 小时运行的唯一启动依赖。

``` text
Browser (Safari / Chrome)
        |
        | http://127.0.0.1:3000
        v
Next.js Web (launchd)
        |
        | REST / WebSocket
        v
FastAPI API (launchd)
        |
        +-------------------------------+
        |                               |
        v                               v
Market Worker                    Recovery / Watchdog
(launchd)                         (launchd)
        |
        v
Strategy + AI Orchestrator + Risk Engine + Execution Worker
        |
        +-------------------+-------------------+
        |                                       |
        v                                       v
PostgreSQL (native service)              Redis (native service)
        |
        v
Binance API / WebSocket  <---->  OKX V5 API / WebSocket
```

本机端口建议：

-   Web：`127.0.0.1:3000`
-   API：`127.0.0.1:8000`
-   PostgreSQL：`127.0.0.1:5432`
-   Redis：`127.0.0.1:6379`

安全边界：

-   所有管理服务默认只绑定 `127.0.0.1`，不得监听 `0.0.0.0`。
-   不做路由器端口转发，不将交易管理 API 暴露到公网。
-   远程管理如有需要，后续通过可信
    VPN/零信任网络实现，不直接开放公网端口。
-   API Secret 使用 macOS Keychain 或由 Keychain
    解锁的应用主密钥加密；Secret 不进入前端、日志或 LLM Prompt。
-   建议使用专门的标准 macOS
    用户运行项目，不用日常管理员账户运行交易进程。

## 4.3.1 16GB 内存预算

以下为目标预算，需以 Activity Monitor、`memory_pressure`
和实际负载测试调整，不是系统强制配额：

  进程/用途                       目标内存预算
  ----------------------------- --------------
  macOS、窗口服务和系统缓存             4--5GB
  Next.js Web                       0.4--0.8GB
  FastAPI                             0.5--1GB
  Market / Strategy Worker            1--1.5GB
  Execution / Recovery Worker         0.5--1GB
  PostgreSQL                          1--1.5GB
  Redis                             0.1--0.3GB
  日志、文件缓存及余量                至少 2GB

16GB 配置的限制与优化：

1.  AI 推理使用云端 API；本机只进行特征计算、策略规则、风险计算和 API
    编排。
2.  首版限制扫描范围，优先监控流动性较好的 USDT 交易对；用 WebSocket
    批量订阅，避免逐币高频 REST 轮询。
3.  不部署 Kafka、Kubernetes、ClickHouse 或大型本地模型。
4.  Worker 采用 AsyncIO
    和有界队列；每个任务必须设置超时、并发上限和队列长度上限。
5.  对 K 线和行情快照设置保留周期；长期归档数据压缩后保存。
6.  内存压力变黄或出现 Swap
    持续增长时，先降低扫描币种数量、历史缓存和并发，而不是关闭风控或对账。

## 4.3.2 开发模式与常驻模式

-   **开发模式：** Docker Compose 启动 PostgreSQL、Redis；Next.js 和
    FastAPI 使用开发服务器，便于 AI 编程工具热更新。
-   **常驻模式：** Next.js、FastAPI、Workers 由 `launchd`
    启动；PostgreSQL、Redis 使用原生 macOS 服务管理。该模式减少 Docker
    Desktop 虚拟机的内存占用。
-   **可选容器常驻模式：** 如果决定使用 Docker
    Desktop，必须启用"登录时启动 Docker Desktop"，Compose
    服务设置健康检查和 `restart: unless-stopped`。注意 Docker Desktop
    的登录启动设置只保证用户登录后启动，不能单独解决 FileVault
    解锁和无人登录冷启动问题。

## 4.4 建议仓库目录

``` text
ai-crypto-terminal/
├── apps/
│   └── web/
│       ├── app/
│       ├── components/
│       ├── features/
│       ├── hooks/
│       ├── lib/
│       └── tests/
├── services/
│   └── api/
│       ├── app/
│       │   ├── api/v1/
│       │   ├── core/
│       │   ├── db/
│       │   ├── models/
│       │   ├── schemas/
│       │   ├── repositories/
│       │   ├── services/
│       │   ├── exchanges/
│       │   │   ├── base.py
│       │   │   ├── binance/
│       │   │   └── okx/
│       │   ├── market_data/
│       │   ├── scanner/
│       │   ├── strategies/
│       │   ├── ai/
│       │   ├── risk/
│       │   ├── execution/
│       │   ├── portfolio/
│       │   ├── journal/
│       │   └── workers/
│       └── tests/
├── packages/
│   └── shared-contracts/
├── infra/
│   ├── docker/
│   └── scripts/
├── docs/
│   ├── PRD.md
│   ├── ARCHITECTURE.md
│   ├── DATABASE.md
│   ├── API.md
│   ├── SECURITY.md
│   ├── TEST_PLAN.md
│   └── RUNBOOK.md
├── data/
├── logs/
├── .env.example
├── docker-compose.yml
├── Makefile
└── README.md
```

------------------------------------------------------------------------

# 5. 数据库设计

## 5.1 数据库设计原则

-   PostgreSQL 作为业务事实存储。
-   所有价格、数量、金额和费率使用 `NUMERIC`，Python 使用 `Decimal`。
-   时间统一使用 UTC `TIMESTAMPTZ`。
-   保留交易所原始 symbol 和统一 symbol。
-   订单、成交和审计记录采用追加式记录，避免无痕覆盖。
-   重要事件保留原始响应 JSONB，但必须过滤敏感字段。
-   使用 Alembic 管理 Schema 版本。
-   对交易所事件设置唯一约束，保证重复推送不会重复入账。

## 5.2 核心表

  -----------------------------------------------------------------------
  表                      关键字段                说明
  ----------------------- ----------------------- -----------------------
  exchange_connections    id, exchange,           交易所连接
                          environment,            
                          encrypted_key_ref,      
                          status                  

  trading_accounts        id, connection_id,      账户及产品资格
                          account_type, region,   
                          capabilities            

  instruments             id, exchange,           交易规则
                          market_type,            
                          native_symbol,          
                          canonical_symbol,       
                          tick_size, lot_size,    
                          contract_size           

  market_candles          instrument_id,          K 线
                          timeframe, open_time,   
                          OHLCV, is_closed        

  market_snapshots        instrument_id,          行情快照
                          event_time, last, bid,  
                          ask, mark, funding_rate 

  strategies              id, name, status,       策略定义
                          config                  

  strategy_versions       id, strategy_id,        策略版本
                          version, parameters,    
                          code_hash               

  strategy_runs           id,                     策略运行
                          strategy_version_id,    
                          started_at, ended_at,   
                          status                  

  agent_runs              id, run_type, model,    Agent 执行
                          prompt_version, status, 
                          latency_ms              

  ai_decisions            id, agent_run_id,       AI 决策
                          market_snapshot_id,     
                          output_json,            
                          schema_version          

  trade_signals           id, strategy_run_id,    信号
                          decision_id,            
                          instrument_id,          
                          direction, expires_at   

  risk_profiles           id, account_id,         风控配置
                          limits_json, enabled    

  risk_events             id, account_id,         风险事件
                          event_type, severity,   
                          details, created_at     

  orders                  id, account_id,         订单
                          signal_id,              
                          client_order_id,        
                          exchange_order_id,      
                          status, side, qty,      
                          price                   

  fills                   id, order_id,           成交
                          exchange_trade_id,      
                          price, qty, fee,        
                          fee_currency,           
                          realized_pnl            

  positions               id, account_id,         当前仓位快照
                          instrument_id,          
                          position_side, qty,     
                          entry_price,            
                          mark_price,             
                          unrealized_pnl          

  balance_snapshots       id, account_id, asset,  余额快照
                          total, available,       
                          locked, observed_at     

  equity_snapshots        id, account_id, equity, 净值快照
                          realized_pnl,           
                          unrealized_pnl,         
                          observed_at             

  transfers               id, account_id,         入金/出金/内部转账
                          transfer_type, asset,   
                          amount, occurred_at     

  audit_logs              id, actor, action,      审计
                          resource, before_json,  
                          after_json, created_at  

  system_events           id, component,          系统事件
                          event_type, severity,   
                          payload, created_at     

  backtest_runs           id,                     回测
                          strategy_version_id,    
                          dataset, parameters,    
                          metrics, status         

  kill_switch_states      id, scope, enabled,     熔断状态
                          reason, changed_at      
  -----------------------------------------------------------------------

## 5.3 关键约束和索引

-   `orders(account_id, client_order_id)` 唯一。
-   `fills(exchange, exchange_trade_id)` 或等价复合键唯一。
-   `market_candles(exchange, market_type, symbol, timeframe, open_time)`
    唯一。
-   `instruments(exchange, market_type, native_symbol)` 唯一。
-   `positions(account_id, instrument_id, position_side)` 建索引。
-   `orders(account_id, status, created_at)` 建索引。
-   `ai_decisions(created_at, strategy_version_id)` 建索引。
-   `audit_logs(created_at, resource)` 建索引。

实际建表时，应补充外键、检查约束、软删除策略和数据保留策略。

## 5.4 订单与持仓数据原则

-   `orders` 表记录委托生命周期。
-   `fills` 表记录每一次真实成交，不以订单平均价替代成交明细。
-   `positions` 是当前状态快照，可从交易所重新同步。
-   盈亏统计应从成交、费用、资金费率和资金转移记录重建或核验。
-   不能只靠 AI 决策表推断实际发生了交易。

------------------------------------------------------------------------

# 6. 内部 API 设计

统一前缀：`/api/v1`

## 6.1 交易所与账户

  Method   Path                                  用途
  -------- ------------------------------------- ----------------
  GET      `/exchanges`                          支持的交易所
  GET      `/exchange-connections`               连接列表
  POST     `/exchange-connections`               新增连接
  POST     `/exchange-connections/{id}/test`     测试连接和权限
  GET      `/exchange-connections/{id}/status`   连接状态
  POST     `/exchange-connections/{id}/sync`     同步账户
  DELETE   `/exchange-connections/{id}`          删除连接
  GET      `/accounts`                           账户列表
  GET      `/accounts/{id}/balances`             账户余额
  GET      `/accounts/{id}/capabilities`         产品资格和权限

## 6.2 行情与 Scanner

  Method   Path                       用途
  -------- -------------------------- --------------
  GET      `/markets/instruments`     交易产品列表
  GET      `/markets/tickers`         最新行情
  GET      `/markets/candles`         K 线
  GET      `/markets/orderbook`       订单簿
  GET      `/markets/funding-rate`    资金费率
  GET      `/scanner/status`          扫描器状态
  GET      `/scanner/opportunities`   候选机会
  GET      `/scanner/rankings`        排名结果
  POST     `/scanner/start`           启动扫描
  POST     `/scanner/pause`           暂停扫描

## 6.3 策略与 AI

  Method   Path                       用途
  -------- -------------------------- -------------------
  GET      `/strategies`              策略列表
  POST     `/strategies`              创建策略
  GET      `/strategies/{id}`         策略详情
  PUT      `/strategies/{id}`         创建/更新配置版本
  POST     `/strategies/{id}/start`   启动策略
  POST     `/strategies/{id}/pause`   暂停策略
  GET      `/agents/status`           Agent 状态
  GET      `/agents/runs`             Agent 执行历史
  GET      `/decisions`               AI 决策列表
  GET      `/trade-signals`           交易信号

## 6.4 风控、订单和持仓

  Method   Path                      用途
  -------- ------------------------- --------------
  GET      `/risk/profile`           获取风控参数
  PUT      `/risk/profile`           修改风控参数
  GET      `/risk/status`            当前风控状态
  GET      `/risk/events`            风险事件
  POST     `/risk/kill-switch`       禁止新开仓
  POST     `/risk/resume`            用户确认恢复
  POST     `/risk/flatten`           尝试紧急平仓
  GET      `/orders`                 订单列表
  GET      `/orders/{id}`            订单详情
  POST     `/orders/{id}/cancel`     撤单
  GET      `/fills`                  成交记录
  GET      `/positions`              当前持仓
  POST     `/positions/{id}/close`   请求平仓
  GET      `/portfolio/summary`      资产汇总

## 6.5 自动化和分析

  Method   Path                                用途
  -------- ----------------------------------- --------------
  GET      `/automation/status`                自动交易状态
  POST     `/automation/start`                 启动自动交易
  POST     `/automation/pause`                 暂停新开仓
  GET      `/analytics/summary`                盈亏摘要
  GET      `/analytics/equity-curve`           净值曲线
  GET      `/analytics/strategy-performance`   策略统计
  GET      `/journal`                          交易复盘
  POST     `/backtests`                        创建回测
  GET      `/backtests/{id}`                   回测结果

## 6.6 WebSocket

-   `/ws/dashboard`
-   `/ws/market`
-   `/ws/scanner`
-   `/ws/agents`
-   `/ws/orders`
-   `/ws/positions`
-   `/ws/risk`
-   `/ws/system`

WebSocket 消息统一包含：

``` json
{
  "event_id": "uuid",
  "event_type": "order.updated",
  "occurred_at": "2026-01-01T00:00:00Z",
  "source": "binance",
  "data": {}
}
```

消息需要有事件
ID、时间戳和来源，前端应处理重复消息、断线重连和状态重新拉取。

------------------------------------------------------------------------

# 7. 交易所 Adapter 设计

## 7.1 统一接口

``` python
from typing import Protocol

class ExchangeAdapter(Protocol):
    async def connect(self): ...
    async def get_capabilities(self): ...
    async def get_instruments(self, market_type): ...
    async def get_balance(self): ...
    async def get_ticker(self, symbol): ...
    async def get_klines(self, symbol, timeframe, limit): ...
    async def get_orderbook(self, symbol): ...
    async def get_positions(self): ...
    async def place_order(self, request): ...
    async def get_order(self, symbol, order_id=None, client_order_id=None): ...
    async def cancel_order(self, symbol, order_id=None, client_order_id=None): ...
    async def get_fills(self, symbol=None, since=None): ...
    async def subscribe_market(self, symbols): ...
    async def subscribe_private(self): ...
    async def reconcile(self): ...
```

## 7.2 Binance Adapter

拆分：

-   Spot REST / WebSocket。
-   USDⓈ-M Futures REST / WebSocket。
-   Account/User Data Stream。
-   Exchange Info 和交易规则缓存。
-   限频、时间同步、签名和错误码处理。

## 7.3 OKX Adapter

使用 V5 API，拆分：

-   Public REST / WebSocket。
-   Private REST / WebSocket。
-   Account、Trade、Market、Public、Status 相关接口。
-   `instType`、`tdMode`、`posSide`、`reduceOnly` 等参数映射。
-   Passphrase、签名、时间戳和模拟交易标识。
-   按账户地区选择正确 API 域名和产品资格。

## 7.4 交易所文档核对

开发时以官方文档当前版本为准，不要将本文中的接口清单视为永久不变的 API
合约：

-   Binance API 文档：<https://developers.binance.com/docs>
-   OKX API 文档：<https://www.okx.com/docs-v5/en/>

每个接口在代码中应有对应的集成测试和文档链接。交易所接口、限频、权限和产品可用性会变化。

------------------------------------------------------------------------

# 8. 交易工作流与状态机

## 8.1 主流程

``` text
行情事件到达
   |
   v
数据标准化与质量检查
   |
   v
Scanner 发现候选
   |
   v
策略规则生成信号
   |
   v
AI 分析并生成 TradePlan
   |
   v
Schema 校验与计划过期检查
   |
   v
Risk Engine 独立审核
   |
   +---- 拒绝 ----> 记录 Risk Event
   |
   v
计算数量、价格和交易规则
   |
   v
生成唯一 client_order_id
   |
   v
Execution Engine 提交订单
   |
   v
交易所订单事件与 REST 对账
   |
   v
成交 / 部分成交 / 撤单 / 拒绝
   |
   v
持仓监控与原生止盈止损
   |
   v
平仓、费用核算和 Journal
```

## 8.2 订单状态

``` text
CREATED
  -> VALIDATED
  -> SUBMITTED
  -> ACKNOWLEDGED
  -> PARTIALLY_FILLED
  -> FILLED

异常状态：
REJECTED
CANCELED
EXPIRED
UNKNOWN
```

`UNKNOWN` 必须作为一等状态处理。请求超时不代表交易所没有收到订单。应先按
client order ID
查询订单、查询成交并同步持仓；只有确认原订单不存在，才允许重新提交。

## 8.3 系统故障处理

  故障                 系统行为
  -------------------- ----------------------------------------
  AI API 不可用        禁止新开仓；已有仓位继续由保护机制管理
  行情断流             标记数据过期，禁止新开仓
  交易所私有 WS 断开   REST 对账；状态不确定时暂停新开仓
  下单请求超时         标记 UNKNOWN，查询原订单，不盲目重试
  PostgreSQL 不可用    禁止新开仓，保留故障告警
  Redis 不可用         停止依赖队列的交易工作流
  Risk Engine 异常     Fail Closed，拒绝新开仓
  Mac 休眠/关机        本机服务停止；恢复后先对账再允许交易
  订单保护单未确认     不得假设止损已生效；报警并进入保护流程

------------------------------------------------------------------------

# 9. Mac 开发与运行环境

## 9.1 推荐环境

-   macOS：近期受支持版本。
-   芯片：Apple Silicon 或 Intel Mac；优先 Apple Silicon。
-   Python：3.12 或项目选定的受支持版本。
-   Node.js：当前受支持的 LTS。
-   Homebrew（常驻模式安装 PostgreSQL、Redis 和辅助工具）。
-   Docker Desktop（仅开发/测试模式需要；16GB 常驻模式优先不依赖它）。
-   Git。
-   VS Code、Cursor 或其他 AI 编程 IDE。
-   浏览器：Chrome、Safari 或 Edge。

具体版本应在项目初始化时锁定，并提交
`.python-version`、`package-lock.json`/`pnpm-lock.yaml` 和 Docker
镜像版本。

## 9.2 本地启动方式

建议提供统一命令：

``` bash
# 1. 克隆项目
git clone <your-repository>
cd ai-crypto-terminal

# 2. 创建环境变量文件
cp .env.example .env

# 3. 启动数据库和 Redis
docker compose up -d postgres redis

# 4. 安装后端依赖
cd services/api
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 5. 执行数据库迁移
alembic upgrade head

# 6. 启动 API
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 7. 新终端启动前端
cd apps/web
npm install
npm run dev
```

最终应提供 `make dev` 或 `./scripts/dev.sh`，将启动步骤封装成一个命令。

访问：

-   前端：`http://localhost:3000`
-   API 文档：`http://127.0.0.1:8000/docs`
-   健康检查：`http://127.0.0.1:8000/health`

## 9.3 Docker Compose 约束

Docker Compose 作为开发和集成测试环境，必须满足：

-   PostgreSQL 和 Redis 端口只绑定 `127.0.0.1`。
-   使用命名 Volume，容器重建不丢数据。
-   设置 healthcheck 和 `depends_on: condition: service_healthy`。
-   设置合理的容器资源上限，避免占满 16GB 统一内存。
-   数据库使用独立用户名和强密码。
-   不将 `.env`、API Key 或密钥提交 Git。
-   Compose 中不得写入真实 API Secret。
-   开发环境与 Live 环境使用不同数据库、不同 API Key 和不同配置文件。
-   生产/实盘模式不得使用开发默认密码。

如使用 Docker Desktop，打开 Settings → General → **Start Docker Desktop
when you sign in to your computer**。Docker Compose 服务可设置
`restart: unless-stopped`，但容器重启策略只有在 Docker Engine
已经运行时才生效。

## 9.4 Mac mini 24小时常驻运行策略

### 9.4.1 推荐运行方式

16GB Mac mini 的首选常驻方案：

-   PostgreSQL：Homebrew 安装的原生服务。
-   Redis：Homebrew 安装的原生服务。
-   Next.js：生产构建后由 Node.js 运行。
-   FastAPI：Python 虚拟环境中的 Uvicorn/Gunicorn（按项目选定方式）。
-   Market Worker、Execution Worker、Recovery Watchdog：独立 Python
    进程。
-   所有业务进程由 `launchd` 管理。
-   AI 模型通过云端 API 调用。
-   Docker Compose 仅用于开发和集成测试。

这样避免长期占用 Docker Desktop 虚拟机资源。Homebrew 的 `brew services`
使用 macOS `launchd` 管理服务；不带 sudo 的服务通常作为当前用户的
LaunchAgent 在登录后运行，使用 sudo 则会安装为系统级
LaunchDaemon。部署时必须审查服务运行用户和文件权限，不得为了方便让交易程序以
root 身份运行。

### 9.4.2 电源与睡眠设置

在 Mac mini 上执行：

1.  打开 **系统设置 → 能源（Energy）**。
2.  开启 **显示器关闭时，防止自动进入睡眠**。
3.  显示器可以在 5--15 分钟后关闭，不需要保持亮屏。
4.  在"能源"设置中，将断电恢复启动行为设为 **断电后启动（After power
    failure）** 或当前 macOS 提供的等效选项。
5.  如使用 UPS，在能源/UPS 选项中配置低电量时安全关机。
6.  让路由器、光猫和 Mac mini 尽可能接入同一台 UPS。

可用以下命令查看当前设置：

``` bash
pmset -g custom
pmset -g assertions
```

在确认是固定供电的 Mac mini
后，可由管理员评估使用以下命令禁止系统闲置睡眠，并启用断电恢复启动：

``` bash
sudo pmset -a sleep 0
sudo pmset -a autorestart 1
```

执行后再次运行 `pmset -g custom`
核对。不要为了让交易程序运行而关闭显示器休眠；显示器关闭与系统睡眠是不同设置。若
macOS 版本或设备不支持某个选项，以系统设置界面显示为准。

### 9.4.3 供电、网络和物理环境

-   使用品质可靠的 UPS，优先选择支持 USB 状态通讯的型号。
-   UPS 同时为 Mac mini、路由器和光猫供电；否则 Mac
    仍可能因网络设备断电而离线。
-   设置 UPS 低电量安全关机，避免文件系统和数据库突然损坏。
-   路由器建议使用稳定连接；如 ISP 支持，可为 Mac 配置 DHCP 保留地址。
-   如果交易所 API 支持 IP 白名单，使用稳定公网出口；公网 IP
    变化时必须报警并更新白名单。
-   保持散热通风，避免将 Mac mini 放在封闭柜体或热源旁。
-   预留充足磁盘空间；磁盘可用空间低于 20% 时报警。
-   重要数据库备份应另存到加密外置 SSD 或安全的异地备份位置。

### 9.4.4 自动启动：launchd

macOS 官方推荐使用 `launchd`
管理后台脚本和服务。常驻用户会话中的服务配置放在：

``` text
~/Library/LaunchAgents/
```

建议建立以下服务：

``` text
com.aicrypto.postgres.plist       # 如果采用原生自管数据库服务
com.aicrypto.redis.plist          # 如果采用原生自管 Redis 服务
com.aicrypto.api.plist
com.aicrypto.web.plist
com.aicrypto.market-worker.plist
com.aicrypto.execution-worker.plist
com.aicrypto.recovery-watchdog.plist
```

每个 plist 至少明确：

-   `Label`：唯一服务标识。
-   `ProgramArguments`：可执行文件和参数，使用绝对路径。
-   `WorkingDirectory`：项目目录。
-   `RunAtLoad = true`：加载后启动。
-   `KeepAlive = true`：进程退出后由 launchd 重新启动。
-   `StandardOutPath`、`StandardErrorPath`：独立日志文件。
-   `ThrottleInterval`：限制异常退出后的重启频率。

示例模板（实际路径必须按本机用户名、Homebrew 前缀、Python 虚拟环境和
Node 版本替换）：

``` xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.aicrypto.api</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/USERNAME/ai-crypto-terminal/services/api/.venv/bin/uvicorn</string>
    <string>app.main:app</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>8000</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/USERNAME/ai-crypto-terminal/services/api</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>ThrottleInterval</key>
  <integer>10</integer>

  <key>StandardOutPath</key>
  <string>/Users/USERNAME/ai-crypto-terminal/logs/api.out.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/USERNAME/ai-crypto-terminal/logs/api.err.log</string>
</dict>
</plist>
```

加载和管理示例：

``` bash
mkdir -p ~/Library/LaunchAgents
launchctl bootstrap gui/$(id -u) \
  ~/Library/LaunchAgents/com.aicrypto.api.plist

launchctl kickstart -k gui/$(id -u)/com.aicrypto.api

launchctl print gui/$(id -u)/com.aicrypto.api

launchctl bootout gui/$(id -u) \
  ~/Library/LaunchAgents/com.aicrypto.api.plist
```

实施要求：

-   不要把上面的示例路径原样复制到生产环境；必须替换为真实绝对路径。
-   `launchd` 负责进程存活，不负责判断交易状态是否安全。
-   API 和 Worker 启动时必须自行等待数据库、Redis 和交易所连接就绪。
-   `KeepAlive` 进程需要内部退避和错误熔断，避免故障时无限快速重启。
-   不要把所有服务塞进一个 Shell
    脚本；每个关键进程独立管理，便于隔离和排障。
-   不要以 root 运行 Next.js、FastAPI 或交易执行器。

### 9.4.5 重启后无人值守恢复的边界

必须区分三种情况：

1.  **普通进程崩溃：** `launchd` 可重新启动进程，Recovery Watchdog
    再检查依赖和交易所状态。
2.  **用户会话内服务重启：** LaunchAgents 可恢复服务，数据库和 Worker
    重新连接。
3.  **Mac 断电后冷启动：**
    是否能完全无人值守取决于断电恢复设置、FileVault、用户登录方式和服务类型。

重要安全事实：

-   Apple 文档说明，启用 FileVault 时 macOS
    自动登录不可用；重启后需要先解锁启动磁盘。
-   `~/Library/LaunchAgents` 只有在用户登录会话建立后才会运行。
-   `LaunchDaemons` 可在系统级运行，但必须设计专用非 root
    服务用户、权限、密钥解锁和日志目录；不能简单把现有 LaunchAgent plist
    移到系统目录就认为部署完成。
-   不建议为了自动登录而关闭 FileVault。V1.1 默认优先保护 API Key
    和交易数据：保持 FileVault
    开启，接受严重断电或系统更新重启后可能需要人工解锁/登录。UPS
    和断电恢复设置用于降低这种情况发生的概率。
-   若未来要求"停电恢复后无人到场、无需人工解锁"，必须单独设计并审查系统级
    LaunchDaemon、密钥管理、磁盘加密和物理安全方案；不得把这一能力作为当前版本已经具备的承诺。

### 9.4.6 故障恢复状态机

Mac 或服务恢复时必须进入 `RECOVERING`，而不是立即继续交易：

``` text
HOST_BOOT
   |
   v
START_DATABASE_AND_REDIS
   |
   v
START_API_AND_WORKERS
   |
   v
RECOVERY_MODE (禁止新开仓)
   |
   v
CHECK_LOCAL_DATABASE
   |
   v
CONNECT_BINANCE_AND_OKX
   |
   v
FETCH_BALANCES / POSITIONS / OPEN_ORDERS / RECENT_FILLS
   |
   v
RECONCILE_LOCAL_AND_EXCHANGE_STATE
   |
   +---- mismatch / unknown ----> KEEP KILL SWITCH ON + ALERT
   |
   v
VERIFY EXCHANGE-NATIVE PROTECTIVE ORDERS
   |
   +---- protection missing ----> KEEP NEW ENTRIES DISABLED + ALERT
   |
   v
READY (恢复行情监控)
   |
   v
ALLOW NEW ENTRIES ONLY IF ALL RISK GATES PASS
```

恢复规则：

-   默认恢复行情监控和已有仓位监控，不自动恢复所有新开仓策略。
-   本地订单状态为 `UNKNOWN` 时，必须查询交易所订单、成交和持仓。
-   不得因本地数据库没有订单记录就推断交易所没有订单。
-   不得在恢复时自动平掉全部仓位。
-   检查交易所原生止损/止盈订单是否仍存在、数量是否匹配、是否仍有效。
-   任何对账差异、行情过期、私有 WebSocket
    未恢复或风险服务异常，都保持禁止新开仓状态。
-   恢复 Live 自动开仓应遵循用户预先设定的恢复策略；V1.1
    默认需要用户在网页确认恢复。

### 9.4.7 监控与告警

建立独立 Watchdog，建议每 30--60 秒检查一次：

  监控对象           检查内容                        异常动作
  ------------------ ------------------------------- ----------------------------
  Web/API            进程、HTTP health、响应时间     重启服务、告警
  PostgreSQL         连接、查询、磁盘空间            禁止新开仓、告警
  Redis              PING、内存、队列积压            暂停依赖队列的交易
  Market Worker      进程、行情时间戳、订阅状态      标记行情过期
  Exchange WS        公共/私有连接、心跳             REST 对账，暂停新开仓
  Execution Worker   订单队列、UNKNOWN 数量          暂停新增订单
  Risk Engine        健康状态、熔断状态              Fail Closed
  AI Provider        超时、错误率、Token 用量        禁止新开仓或降级为规则监控
  Mac 主机           内存压力、CPU、磁盘、运行时间   告警、降低扫描负载

建议告警阈值（可配置）：

-   磁盘可用空间低于 20%。
-   内存压力持续处于黄色/红色。
-   API 或 Worker 连续健康检查失败。
-   行情数据超过策略允许的新鲜度阈值。
-   交易所私有 WebSocket 断开。
-   订单 `UNKNOWN` 超过处理时限。
-   本地与交易所持仓不一致。
-   日亏损达到风险阈值。
-   备份失败或最近一次成功备份超时。

健康接口：

-   `/health/live`：进程是否存活。
-   `/health/ready`：依赖是否就绪、是否允许交易。
-   `/metrics`：内部指标，仅本机访问。

告警渠道：

-   macOS 通知（用户会话可用时）。
-   邮件或 Telegram/Webhook（需要单独配置）。
-   可选外部心跳服务：Mac 定期发送心跳，若 Mac
    整机断电或网络断开，由外部服务通知用户。单纯本机日志无法报告整机离线。

### 9.4.8 备份与恢复

-   PostgreSQL 每日执行加密备份。
-   至少保留 7 份每日备份和 4 份每周备份。
-   备份复制到外置 SSD 或安全异地存储，不只放在同一块内置磁盘。
-   每月做一次恢复演练，验证数据库可恢复、订单与成交记录完整。
-   备份中不得包含明文 API Secret。
-   Keychain 主密钥的恢复流程必须单独记录并离线保管恢复材料。
-   备份任务失败必须触发告警。
-   日志轮转，避免日志占满磁盘；交易审计日志保留策略与普通调试日志分开。

### 9.4.9 安全与运维命令

建议提供统一 Makefile 命令：

``` bash
make dev                 # 开发模式
make install-services    # 安装/更新 launchd 配置
make start               # 启动常驻服务
make stop                # 停止服务（不等于平仓）
make status              # 查看进程、依赖和交易所连接
make logs                # 查看服务日志
make backup              # 执行数据库备份
make restore-check       # 在隔离数据库验证备份
make reconcile           # 手动执行交易所对账
make kill-switch         # 禁止新开仓
```

`make stop`
不得被设计成自动撤单或自动平仓。停止服务前应先暂停新开仓、确认保护订单仍在交易所生效，并明确提示用户当前持仓状态。

## 9.5 Mac mini 24小时运行验收

常驻模式正式启用前，必须完成：

-   [ ] 显示器关闭但 Mac 不睡眠，连续运行测试通过。
-   [ ] Docker Desktop
    不是常驻模式的必需依赖（除非用户明确选择容器常驻模式）。
-   [ ] API、Web、Market、Execution、Watchdog 均由 launchd 管理。
-   [ ] 杀掉单个 Worker 后能够按预期恢复。
-   [ ] PostgreSQL 或 Redis 不可用时禁止新开仓。
-   [ ] 网络断开后禁止新开仓，恢复后自动对账。
-   [ ] 下单响应超时进入 UNKNOWN 查询流程。
-   [ ] 模拟 Mac 重启后先恢复数据库和服务，再进行交易所对账。
-   [ ] 模拟 UPS 低电量关机流程，数据库正常关闭。
-   [ ] 备份可恢复。
-   [ ] 内存压力持续为绿色，磁盘空间有余量。
-   [ ] 外部心跳能检测整机离线。
-   [ ] Live Trading 默认关闭。

------------------------------------------------------------------------

# 10. AI 编程工具协作规范

本项目主要使用 AI 编程工具开发。为了避免 AI
一次生成大量无法运行的代码，必须采用小任务、短反馈、可测试的开发方式。

## 10.1 AI 编程工作原则

1.  每次只让 AI 完成一个明确模块或一个小里程碑。
2.  先让 AI 阅读 `README.md`、`ARCHITECTURE.md` 和相关模块代码。
3.  要求先给出修改计划，再写代码。
4.  每次修改必须包含测试。
5.  不允许 AI 自行修改不相关模块。
6.  不允许 AI 擅自更改数据库结构而不生成 Alembic Migration。
7.  不允许 AI 把真实 API Key 写进代码、测试数据、日志或 Prompt。
8.  不允许 AI 在未经确认时启用 Live Trading。
9.  不允许 AI 为了"让测试通过"而删除风控逻辑。
10. 每个阶段结束必须运行格式化、类型检查、单元测试和集成测试。

## 10.2 推荐开发循环

``` text
任务说明
  -> AI 阅读现有代码
  -> AI 提出实现计划
  -> 人工确认边界
  -> AI 编码
  -> AI 编写测试
  -> 本地运行测试
  -> 人工审查 Diff
  -> 合并代码
  -> 更新文档
```

## 10.3 项目根目录 AI_RULES.md

建议建立 `AI_RULES.md`，内容至少包括：

``` text
# Project Rules

1. Read README.md and docs/ARCHITECTURE.md before coding.
2. Follow the existing module boundaries.
3. Never expose API secrets to frontend code or LLM prompts.
4. Never enable live trading by default.
5. All orders must pass the deterministic Risk Engine.
6. Never retry an order blindly after a timeout.
7. Use Decimal for money, price, quantity and fee calculations.
8. Use UTC timestamps.
9. Every database change requires an Alembic migration.
10. Every feature requires tests.
11. Never remove or weaken a risk rule to make tests pass.
12. Use exchange demo/testnet environments for integration tests.
13. Do not invent exchange API fields; verify against official docs.
14. Keep secrets out of logs, snapshots and error messages.
15. Update relevant docs when API contracts or schemas change.
```

## 10.4 可直接交给 AI 编程工具的总任务提示词

``` text
你是本项目的资深全栈工程师和交易系统工程师。

项目：AI Crypto Trading Terminal
产品形态：个人使用的本地 Web 应用
运行环境：macOS
前端：Next.js + TypeScript
后端：Python + FastAPI
数据库：PostgreSQL
缓存：Redis
部署：macOS launchd + Homebrew 原生服务；Docker Compose 用于开发/测试
交易所：Binance、OKX
市场：Spot、USDT Perpetual
模式：先 Demo/Testnet，实盘默认关闭

工程规则：
1. 先阅读项目文档和现有代码。
2. 每次只实现一个小任务。
3. 修改前先列出文件和实现步骤。
4. 使用 Decimal 处理所有资金、价格和数量。
5. 所有交易必须经过 Risk Engine。
6. AI 只能输出结构化 TradePlan，不能直接访问交易所下单接口。
7. API Secret 必须加密，不能进入前端或 LLM Prompt。
8. 下单超时必须先查询订单，不允许盲目重试。
9. 每次数据库变更必须生成 Migration。
10. 每个模块必须有单元测试和必要的集成测试。
11. 默认只连接 Demo/Testnet。
12. 未经明确批准不得开启 Live Trading。
13. 不得编造交易所 API 字段或接口。
14. 发现交易所规则不确定时，先查官方文档并报告。
15. 完成后提供修改文件列表、运行命令、测试结果和未解决风险。

现在先不要一次性生成整个项目。
请先完成阶段 0：检查开发环境、创建项目目录、初始化 Git、编写 README、ARCHITECTURE、AI_RULES 和 .env.example。
完成后停止，等待验收。
```

------------------------------------------------------------------------

# 11. 分阶段实施计划与验收标准

以下按一个熟悉 AI
编程工具、能够进行代码审查和本地调试的个人开发者估算。AI
编程工具可以加速脚手架和常规
CRUD，但交易所适配、订单状态、风控和测试仍需要认真验证。

## 阶段 0：项目初始化与交易所资格确认

**时间：2--4 天**

任务：

-   [ ] 确认 Mac 芯片、macOS、磁盘空间。
-   [ ] 安装 Git、Node、Python、Homebrew。
-   [ ] 安装 Docker Desktop（仅开发/测试模式需要）。
-   [ ] 确认 FileVault 状态、Mac mini 能源设置和断电恢复选项。
-   [ ] 确认 UPS、路由器和光猫供电方案。
-   [ ] 创建 Git 仓库。
-   [ ] 初始化前后端目录。
-   [ ] 编写 README、AI_RULES 和架构文档。
-   [ ] 确认 Binance API 账户权限。
-   [ ] 确认 OKX API 账户权限及地区产品资格。
-   [ ] 确认 Demo/Testnet 的可用产品。
-   [ ] 确定首版交易对和时间周期。

交付：

-   可运行的空项目。
-   技术决策记录。
-   交易所权限检查清单。
-   本地开发环境文档。

验收：新环境按 README 能完成项目初始化。

## 阶段 1：本地基础工程

**时间：1 周**

任务：

-   [ ] Next.js 项目。
-   [ ] FastAPI 项目。
-   [ ] PostgreSQL。
-   [ ] Redis。
-   [ ] Docker Compose 开发环境。
-   [ ] Homebrew 原生 PostgreSQL/Redis 常驻方案。
-   [ ] launchd 服务模板和本地启动脚本。
-   [ ] Alembic。
-   [ ] 前后端健康检查。
-   [ ] REST API 基础封装。
-   [ ] WebSocket 基础连接。
-   [ ] 暗色 UI 布局和左侧导航。
-   [ ] 本地日志和异常处理。
-   [ ] launchd 自动启动和 KeepAlive。
-   [ ] Watchdog 健康检查和 RECOVERING 状态。

交付：

-   浏览器打开 Dashboard。
-   前后端正常通信。
-   数据库和 Redis 正常运行。
-   一条命令启动开发环境。

验收：重启服务后数据库数据不丢失；前端能够显示 API 健康状态。

## 阶段 2：交易所只读接入

**时间：1--2 周**

任务：

-   [ ] ExchangeAdapter 抽象接口。
-   [ ] Binance 公共行情。
-   [ ] Binance 账户只读。
-   [ ] OKX 公共行情。
-   [ ] OKX 账户只读。
-   [ ] 交易对标准化。
-   [ ] 交易规则同步。
-   [ ] 余额和持仓同步。
-   [ ] WebSocket 断线重连。
-   [ ] REST 对账。
-   [ ] API 密钥本机加密存储。
-   [ ] 权限和账户资格检查。

交付：

-   Exchanges 页面。
-   Markets 页面。
-   Portfolio 页面。
-   两个 Adapter 的集成测试。

验收：

-   两家交易所账户信息可读取。
-   交易对精度和合约面值正确。
-   断线恢复后状态可重新同步。
-   Secret 不会出现在前端或日志。

## 阶段 3：Scanner 与基础策略

**时间：1--2 周**

任务：

-   [ ] K 线历史数据存储。
-   [ ] RSI、MACD、EMA、ATR 指标。
-   [ ] 成交量相对变化。
-   [ ] 波动率和价差计算。
-   [ ] 订单簿深度摘要。
-   [ ] 市场过滤器。
-   [ ] Scanner 任务调度。
-   [ ] 趋势跟随策略。
-   [ ] 突破策略。
-   [ ] 信号去重和过期机制。
-   [ ] 策略版本管理。

交付：

-   Scanner 页面。
-   Strategies 页面。
-   Signals 页面。
-   指标计算测试。

验收：

-   Scanner 不超过交易所限频。
-   同一信号不会反复触发订单。
-   所有信号可以追溯到行情快照和策略版本。

## 阶段 4：AI Agent 与 TradePlan

**时间：1 周**

任务：

-   [ ] AI Provider 抽象层。
-   [ ] 模型配置管理。
-   [ ] Market Agent。
-   [ ] Strategy Agent。
-   [ ] Risk Explanation Agent。
-   [ ] Journal Agent。
-   [ ] Pydantic TradePlan Schema。
-   [ ] Prompt 版本管理。
-   [ ] AI 超时和重试策略。
-   [ ] Token 和调用成本统计。
-   [ ] 决策日志。

交付：

-   AI Agents 页面。
-   AI 决策记录。
-   TradePlan 预览。
-   AI 输出验证测试。

验收：

-   AI 输出始终经过 Schema 校验。
-   AI 不接触交易所密钥。
-   AI 无响应时系统不会自动开仓。
-   AI 不能调用绕过风控的下单路径。

## 阶段 5：风险引擎和模拟交易执行

**时间：2 周**

任务：

-   [ ] Risk Profile。
-   [ ] 仓位和风险预算计算。
-   [ ] 价格和数量精度处理。
-   [ ] 订单状态机。
-   [ ] client_order_id 幂等。
-   [ ] Binance Demo/Testnet 下单。
-   [ ] OKX Demo Trading 下单。
-   [ ] 现货买卖。
-   [ ] USDT 永续开仓和平仓。
-   [ ] 部分成交。
-   [ ] 撤单。
-   [ ] 原生止盈止损保护。
-   [ ] UNKNOWN 订单恢复。
-   [ ] 风险事件记录。
-   [ ] Kill Switch。
-   [ ] 全部持仓对账。

交付：

-   Orders 页面。
-   Positions 页面。
-   Risk Center。
-   模拟交易执行器。
-   故障注入测试。

验收：

-   每笔订单都有唯一 client_order_id。
-   超时不会造成重复下单。
-   风控拒绝订单时不会发送交易请求。
-   平仓方向和数量经过验证。
-   止损订单必须确认交易所已接受。
-   紧急停止能阻止新开仓。

## 阶段 6：回测、模拟运行和复盘

**时间：2 周**

任务：

-   [ ] 历史数据导入。
-   [ ] 回测引擎。
-   [ ] 手续费模型。
-   [ ] 滑点模型。
-   [ ] 永续资金费率模型。
-   [ ] 样本外验证。
-   [ ] Equity Curve。
-   [ ] Drawdown。
-   [ ] Profit Factor。
-   [ ] Journal。
-   [ ] 交易所账单核对。
-   [ ] 7--14 天模拟运行。
-   [ ] 重启恢复测试。
-   [ ] 断网和 API 故障测试。

交付：

-   Analytics 页面。
-   Journal 页面。
-   Backtest 页面。
-   模拟盘验收报告。

验收：

-   本地账本与交易所模拟账户可核对。
-   重启后订单和仓位恢复正确。
-   费用、资金费率和盈亏统计有明确依据。
-   没有未解决的重复下单和状态不一致问题。

## 阶段 7：小额实盘准备与上线

**时间：1--2 周以上**

前置条件：

-   所有 P0 验收通过。
-   交易所账户和产品资格确认。
-   API Key 只有必要权限。
-   提现权限关闭。
-   交易所 IP 白名单配置完成（如适用）。
-   Kill Switch 测试通过。
-   备份和恢复测试通过。
-   实盘模式有显式确认。
-   用户已了解实盘可能发生超过预设风险预算的损失。

任务：

-   [ ] Live 环境与 Demo 环境严格隔离。
-   [ ] 实盘 API 连接检查。
-   [ ] 实盘下单金额限制。
-   [ ] 首次实盘人工确认。
-   [ ] 小额交易验证。
-   [ ] 订单和成交对账。
-   [ ] 费用核算。
-   [ ] 异常报警。
-   [ ] 每日审计报告。

上线原则：

-   Live 默认关闭。
-   不允许 AI 自行切换 Demo 到 Live。
-   不允许 AI 修改最大亏损和杠杆上限。
-   实盘启动必须由用户在界面主动确认。
-   初期只启用一套策略和少量交易对。
-   任何对账异常都暂停新开仓。

------------------------------------------------------------------------

# 12. 测试计划

## 12.1 单元测试

重点测试：

-   Decimal 仓位计算。
-   手续费计算。
-   合约面值换算。
-   tick size 和 lot size 舍入。
-   止损方向校验。
-   最大敞口计算。
-   每日亏损统计。
-   AI JSON Schema。
-   订单状态迁移。
-   client_order_id 幂等。
-   盈亏和资金费率核算。

## 12.2 集成测试

-   Binance Demo/Testnet。
-   OKX Demo Trading。
-   行情订阅。
-   私有账户推送。
-   下单、撤单和查询。
-   部分成交。
-   条件单和止损保护。
-   重连和恢复。
-   限频处理。
-   订单对账。

## 12.3 故障测试

必须模拟：

-   AI 超时。
-   AI 输出非法 JSON。
-   公共行情断线。
-   私有 WebSocket 断线。
-   REST 请求超时。
-   下单响应丢失。
-   数据库重启。
-   Redis 重启。
-   Worker 被强制终止。
-   Mac 重启。
-   系统时间偏差。
-   交易所拒单。
-   部分成交后撤单。
-   止损保护订单被拒绝。

## 12.4 实盘前必须满足的质量门槛

-   没有已知的重复下单缺陷。
-   没有绕过 Risk Engine 的调用路径。
-   订单 UNKNOWN 恢复流程通过测试。
-   本地持仓和交易所持仓可核对。
-   API Secret 不出现在日志和前端。
-   Demo 连续运行期间没有未解释的订单差异。
-   Kill Switch 和重启恢复测试通过。

------------------------------------------------------------------------

# 13. 页面信息架构

左侧导航：

``` text
AI TRADING TERMINAL
│
├── Overview
│
├── EXCHANGES
│   ├── Binance
│   └── OKX
│
├── MARKETS
│   ├── Market Overview
│   ├── Watchlist
│   └── Scanner
│
├── AI SYSTEM
│   ├── Agent Monitor
│   ├── AI Decisions
│   └── Logs
│
├── TRADING
│   ├── Strategies
│   ├── Positions
│   ├── Orders
│   └── Fills
│
├── RISK CENTER
│   ├── Risk Profile
│   ├── Risk Events
│   └── Kill Switch
│
├── ANALYTICS
│   ├── Performance
│   ├── Equity Curve
│   ├── Backtests
│   └── Journal
│
└── SETTINGS
    ├── API Connections
    ├── AI Providers
    ├── Notifications
    └── System
```

页面视觉方向：

-   深色背景。
-   紧凑信息密度。
-   绿色表示正向变化，红色表示负向变化。
-   所有实时状态显示最后更新时间。
-   重要风险告警不能只用颜色表达，必须同时有文字。
-   订单、仓位和账户状态提供筛选和搜索。
-   关键操作（Live、Flatten、Resume）需要二次确认。

------------------------------------------------------------------------

# 14. 项目管理与交付方式

## 14.1 Git 分支

-   `main`：稳定版本。
-   `develop`：集成开发。
-   `feature/*`：功能分支。
-   `fix/*`：缺陷修复。

每个阶段打一个版本标签，例如：

-   `v0.1-foundation`
-   `v0.2-exchanges-readonly`
-   `v0.3-scanner`
-   `v0.4-ai-plans`
-   `v0.5-paper-execution`
-   `v0.6-analytics`
-   `v1.0-release-candidate`

## 14.2 每个功能的 Definition of Done

一个任务只有满足以下条件才能视为完成：

1.  代码已实现。
2.  单元测试通过。
3.  相关集成测试通过。
4.  错误和异常路径有处理。
5.  不泄露敏感信息。
6.  文档已更新。
7.  前端状态和后端状态一致。
8.  数据库 Migration 已提交（如有）。
9.  代码 Diff 已人工审查。
10. 没有意外启用 Live Trading。

## 14.3 每周交付节奏

每周结束时输出：

-   已完成任务。
-   未完成任务。
-   测试结果。
-   新增风险。
-   数据库变化。
-   API 变化。
-   下一周计划。
-   当前版本可运行说明。

------------------------------------------------------------------------

# 15. 风险清单

  -------------------------------------------------------------------------------
  风险               影响               缓解措施
  ------------------ ------------------ -----------------------------------------
  交易所 API 变更    行情或下单失败     Adapter 隔离、契约测试、官方文档复核

  账户地区限制       部分产品不可用     开发前检查账户资格

  API Key 泄露       账户资产风险       Keychain、最小权限、日志脱敏

  AI 幻觉            错误交易计划       Schema、确定性规则、Risk Engine

  重复下单           非预期仓位         幂等 ID、UNKNOWN 状态、对账

  行情延迟           错误入场           数据新鲜度检查、Fail Closed

  网络中断           订单状态未知       重连、REST 对账、暂停新开仓

  Mac 休眠/断电/重启 自动交易中断       Energy
                                        设置、UPS、launchd、恢复对账；FileVault
                                        冷启动可能需要人工解锁

  回测过拟合         实盘表现差         样本外验证、费用和滑点建模

  极端行情           超过预设损失       杠杆限制、保护单、熔断；不承诺损失上限

  AI API 成本        运行成本增加       规则先筛选、调用预算、缓存

  数据库损坏         账本丢失           自动备份、恢复演练、追加式交易记录
  -------------------------------------------------------------------------------

------------------------------------------------------------------------

# 16. 最终交付物

V1.0 应交付：

1.  可运行的 Next.js 网页端。
2.  FastAPI 后端。
3.  Binance Adapter。
4.  OKX Adapter。
5.  PostgreSQL 数据库和 Migration。
6.  Redis 队列和缓存。
7.  Scanner。
8.  基础策略引擎。
9.  AI Agent 工作流。
10. Risk Engine。
11. 模拟交易执行器。
12. 订单、成交和持仓同步。
13. Dashboard。
14. Risk Center。
15. Analytics 和 Journal。
16. 回测模块。
17. Docker Compose 开发环境。
18. launchd plist、安装/卸载脚本和 Watchdog。
19. Mac mini 24小时运行与恢复 Runbook。
20. `.env.example`。
21. `README.md`。
22. `AI_RULES.md`。
23. 架构、数据库、API、安全和测试文档。
24. 模拟盘验收报告。
25. 实盘上线检查清单。

------------------------------------------------------------------------

# 17. 建议立即开始的第一个开发 Sprint

不要让 AI 编程工具一次性生成所有模块。第一轮只做项目骨架。

## Sprint 0：初始化

任务顺序：

1.  创建 Git 仓库 `ai-crypto-terminal`。
2.  创建 `apps/web` Next.js 项目。
3.  创建 `services/api` FastAPI 项目。
4.  创建 Docker Compose 开发环境，并配置 Homebrew 原生 PostgreSQL/Redis
    常驻模式。
5.  配置 `.env.example`。
6.  配置 Alembic。
7.  建立 `/health` 接口。
8.  建立前端 Overview 空页面。
9.  建立本地启动脚本。
10. 编写 README 和 AI_RULES。
11. 编写 launchd plist 模板和服务安装脚本。
12. 编写健康检查、日志目录和恢复状态机骨架。

Sprint 0 验收命令：

``` bash
docker compose up -d postgres redis
make dev
```

验收结果：

-   浏览器能打开 `http://localhost:3000`。
-   `http://127.0.0.1:8000/health` 返回健康状态。
-   PostgreSQL 连接成功。
-   Redis 连接成功。
-   服务重启后数据库数据保留。
-   Git 仓库没有任何真实密钥。

## Sprint 0 完成后，下一轮才开始 Binance 和 OKX 只读接入。

------------------------------------------------------------------------

# 18. 官方技术文档

开发时优先使用官方资料，具体接口以开发时最新版本为准：

-   Binance Developers：<https://developers.binance.com/docs>
-   OKX API V5：<https://www.okx.com/docs-v5/en/>
-   FastAPI：<https://fastapi.tiangolo.com/>
-   Next.js：<https://nextjs.org/docs>
-   PostgreSQL：<https://www.postgresql.org/docs/>
-   SQLAlchemy：<https://docs.sqlalchemy.org/>
-   Alembic：<https://alembic.sqlalchemy.org/>
-   Redis：<https://redis.io/docs/>
-   Docker Compose：<https://docs.docker.com/compose/>
-   TradingView Lightweight
    Charts：<https://tradingview.github.io/lightweight-charts/>

------------------------------------------------------------------------

# 结论

本项目最合理的实施路径是：

**先完成 Mac 本机网页终端和交易所只读接入，再完成 Scanner
与策略，随后开发 AI TradePlan 和独立风控，最后实现 Demo
自动交易、对账、回测和小额实盘准备。**

第一版最重要的不是 AI
能否给出看起来聪明的买卖建议，而是每一笔订单都可追踪、每一次风险判断都可解释、每一次异常都能安全停止，并且本地账本始终能与交易所实际状态核对。

在这些基础能力通过验收前，保持 Live Trading 关闭。

------------------------------------------------------------------------

# 19. Mac mini 16GB 专属架构决策记录（V1.1）

## 19.1 已确定的硬件和产品约束

-   设备：Mac mini。
-   芯片：Apple Silicon。
-   统一内存：16GB。
-   产品：个人使用的本地 Web 交易终端。
-   市场：Binance、OKX 现货和符合账户资格的 USDT 本位永续。
-   运行目标：日常 24 小时常驻。
-   AI：使用云端模型 API，本机不运行大型模型。
-   默认环境：Demo/Testnet；Live 必须单独显式启用。

## 19.2 最终架构决策

**生产常驻：原生 macOS 进程 + launchd + Homebrew 数据服务。**

**开发测试：Docker Compose + Next.js/FastAPI 开发服务器。**

原因：

1.  16GB 统一内存需要尽量减少 Docker Desktop 虚拟机开销。
2.  launchd 是 macOS 原生服务管理机制，适合管理常驻进程和异常重启。
3.  原生进程更容易读取 macOS Keychain、系统通知和本机日志。
4.  数据库、交易 Worker 和 Web 进程可以独立重启。
5.  开发模式仍保留 Docker Compose，便于 AI 编程工具复现环境。

## 19.3 上线前必须确认的系统选项

-   系统设置 → 能源 → 显示器关闭时防止自动进入睡眠。
-   系统设置 → 能源 → 断电后自动启动。
-   系统设置 → 隐私与安全性 → FileVault 状态。
-   系统设置 → 通用 → 登录项与扩展。
-   系统设置 → 网络 → 本机网络稳定性。
-   确认 Mac mini 不会因为计划任务、系统更新或手动操作进入睡眠。
-   确认 UPS 的安全关机行为和恢复供电行为。

## 19.4 FileVault 与完全无人值守的取舍

Apple 明确说明：启用 FileVault 时，自动登录不可用。用户登录会话中的
LaunchAgent 也必须等用户登录后才运行。

因此 V1.1 采取以下安全优先策略：

-   保持 FileVault 开启。
-   Mac
    正常运行期间保持项目专用用户会话登录，锁屏或关闭显示器不应让系统睡眠。
-   进程异常由 launchd 自动恢复。
-   若整机断电、系统更新重启或启动盘锁定，可能需要人工解锁/登录后
    LaunchAgent 才会启动。
-   不为追求"无人到场冷启动"而默认关闭 FileVault。
-   如果未来确实要求无人解锁冷启动，再单独设计
    LaunchDaemon、专用服务账户和密钥托管方案，并进行安全评审。

**产品文案不得声称 Mac mini 已实现 100% 无人值守高可用。**
单机仍有电源、网络、硬件、系统更新和交易所服务中断风险。

## 19.5 常驻模式验收运行时长

进入小额实盘前建议：

1.  Demo/Testnet 连续运行至少 7--14 天。
2.  至少完成一次 API Worker 崩溃恢复测试。
3.  至少完成一次网络断线恢复和交易所对账。
4.  至少完成一次 Mac 正常重启恢复演练。
5.  至少完成一次数据库备份恢复演练。
6.  检查内存压力、Swap、CPU、磁盘和日志增长。
7.  检查外部心跳和告警能否在 Mac 整机离线时通知用户。
8.  所有 UNKNOWN 订单、未对账持仓和保护单异常必须清零或有明确处置记录。

## 19.6 官方参考资料

-   Apple：Mac 台式电脑能源设置\
    <https://support.apple.com/guide/mac-help/mchlp1168/mac>
-   Apple：Mac 睡眠与唤醒设置\
    <https://support.apple.com/guide/mac-help/mchle41a6ccd/mac>
-   Apple：使用 launchd 管理脚本\
    <https://support.apple.com/guide/terminal/script-management-with-launchd-apdc6c1077b/mac>
-   Apple：Service Management、LaunchAgents 和 LaunchDaemons\
    <https://developer.apple.com/documentation/servicemanagement>
-   Apple：FileVault 开启时自动登录不可用\
    <https://support.apple.com/102316>
-   Docker：Docker Desktop 启动和资源设置\
    <https://docs.docker.com/desktop/settings-and-maintenance/settings/>
-   Homebrew：brew services 与 launchd\
    <https://docs.brew.sh/Manpage>
