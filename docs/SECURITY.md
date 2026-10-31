# 安全规范文档 (SECURITY.md)

## 1. 密钥生命周期保护
- **最小权限原则**：仅允许申请 `Read` 与 `Trade` 权限；若连接测试发现 `can_withdraw=True`，系统立即将连接标记为 `REJECTED_WITHDRAW_PERMISSION`，拒绝用于任何自动交易并生成高危风险事件。
- **加密存储**：API Key、Secret、Passphrase 使用 `Fernet` (AES-128-CBC + HMAC-SHA256) 加密后存入数据库，主密钥优先保存在系统级凭据管理器（macOS Keychain / Windows Credential Manager）中。
- **脱敏输出**：所有 REST API、WebSocket 消息、结构化日志、异常堆栈与审计快照均经过敏感词过滤器（`redact_secrets`），API Key 仅展示前 4 位与后 4 位掩码（如 `bina****9f2a`）。

## 2. AI 权限隔离
- AI Agent 仅接收脱敏后的行情特征、K 线摘要、技术指标、账户风险摘要与持仓摘要。
- AI Agent 无权访问数据库凭据、交易所密钥、签名器或任何 Shell/代码执行接口。
- AI 输出仅作为待审核的 `TradePlan` 候选提案，必须通过 Pydantic Schema 校验与独立确定性 `RiskEngine` 审核。

## 2.1 AI Provider 密钥与调用安全
- `AI_API_KEY` 只存在于 `.env`（已加入 `.gitignore`）与进程内存中；**不写数据库、不写日志、不回传任何 API 响应**。
- `GET /api/v1/agents/provider` 只返回脱敏描述（`base_url_masked`、`model`、`timeout`、`retries`、`configured`），可安全展示在页面。
- 每次调用前对 payload 再执行一次 `redact_sensitive_data`（纵深防御），即使上游字段命名错误也不会外泄交易所密钥。
- Provider 采用 `httpx` + `trust_env=False`，避免被宿主机 `HTTP_PROXY` 环境变量劫持到未知出口。
- 错误信息保留上游 HTTP 状态码与 ≤240 字符原因，便于排障，但**从不回显 Authorization 头或密钥**。
- 自动化测试通过 `tests/conftest.py` 强制 `AI_API_KEY=""`，单元测试**永不产生真实 LLM 调用与费用**；Provider 适配器用 `httpx.MockTransport` 独立测试。
- 审计脚本 `infra/scripts/audit-secret-leak.py` 可扫描全部数据表，验证密钥未落库。

## 3. 本机网络与实盘隔离
- 后端默认仅绑定 `127.0.0.1:8000`，PostgreSQL 与 Redis 容器仅绑定 `127.0.0.1`。
- `Demo/Testnet` 与 `Live` 环境严格隔离；`Live` 模式默认关闭，开启前必须通过 9 项上线检查清单并输入显式确认口令。
