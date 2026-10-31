# 系统架构文档 (ARCHITECTURE.md)

## 1. 架构模式
采用 **模块化单体（Modular Monolith）+ 后台异步任务调度（Background Workers）** 架构，通过本地 loopback 网络通信：
- **Web 前端**：Next.js + React + TypeScript + Tailwind CSS（运行于 `http://localhost:3000`）。
- **后端服务**：Python 3.12 + FastAPI + SQLAlchemy 2.0 + Pydantic v2（运行于 `http://127.0.0.1:8000`）。
- **存储与事件**：PostgreSQL（支持 SQLite Decimal 本地无缝降级）+ Redis（支持进程内异步事件总线降级）。

## 2. 核心数据流与控制流

```text
[Exchange WS / REST]
        │
        ▼
[Market Data Service] ──(标准化 canonical_symbol, UTC, is_closed, is_stale)──► [Database / Cache]
        │
        ▼
[Market Scanner] ──(计算 EMA/RSI/MACD/ATR/Spread/Imbalance 并过滤)──► [Candidate Opportunities]
        │
        ▼
[Strategy Engine] ──(趋势跟随 / 区间突破规则匹配 + 信号去重)──► [TradeSignal]
        │
        ▼
[AI Orchestrator] ──(Market -> Scanner -> Strategy -> Risk Explanation Agent)──► [TradePlan (Pydantic 校验)]
        │
        ▼
[Deterministic Risk Engine] ──(10 大硬性风控规则 + Decimal 仓位与精度计算)
        │
        ├── 拒绝 ──► 记录 RiskEvent & AuditLog (不发送任何下单请求)
        │
        ▼ 通过
[Execution Engine] ──(生成唯一 client_order_id, 状态机 CREATED -> VALIDATED -> SUBMITTED)
        │
        ▼
[Exchange Adapter (Binance / OKX Demo or Live)]
        │
        ├── 超时/网络未知 ──► 标记 UNKNOWN ──► 查询原订单与对账 (严禁盲目重试)
        │
        ▼ 成交/确认
[Portfolio & Reconciliation + Native TP/SL Verification + Journal Agent]
```

## 3. 模块边界约束
- `app/ai/` 严禁导入 `app/exchanges/` 的下单方法或读取 `app/core/security.py` 的解密方法。
- `app/execution/` 只接受带有 `RiskCheckResult(approved=True)` 签章的订单请求。
- 所有金额、价格、数量运算统一通过 `app/core/precision.py` 中的 `Decimal` 工具函数完成。
