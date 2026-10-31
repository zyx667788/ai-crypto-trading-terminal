# 运维与故障处理手册 (RUNBOOK.md)

## 1. 数据库要求与初始化 (Database Policy & Setup)

生产环境与多进程 Worker 架构**强制要求使用 PostgreSQL 14+**。由于多进程架构下（API、Market Worker、Execution Worker、Recovery Watchdog）依赖 PostgreSQL 的行级排他锁与事务 Outbox 消费机制（`FOR UPDATE SKIP LOCKED`），SQLite 无法提供跨进程锁语义，因此 Worker 进程启动时会执行数据库策略检查并拒绝在未授权的 SQLite 环境下启动。

### 1.1 PostgreSQL 初始化步骤 (PostgreSQL Setup)
```bash
# 1. 初始化数据库集群（如未安装服务）
initdb -D /usr/local/var/postgres

# 2. 创建数据库用户与专有数据库
createuser -s terminal_user
createdb -O terminal_user ai_crypto_terminal

# 设置密码 (在 psql 或通过 ALTER USER)
# psql -d ai_crypto_terminal -c "ALTER USER terminal_user WITH ENCRYPTED PASSWORD 'your_password';"

# 3. 配置连接字符串格式 (PostgreSQL 14+ / psycopg v3 driver)
# postgresql+psycopg://<username>:<password>@<host>:<port>/<dbname>
export DATABASE_URL="postgresql+psycopg://terminal_user:your_password@127.0.0.1:5432/ai_crypto_terminal"

# 4. 执行 Alembic 数据库迁移同步至最新 Head
cd services/api
alembic upgrade head
```

### 1.2 本地单进程实验与降级 (Local Escape Hatch)
若在本地单进程调试或自动化单测中需要临时使用 SQLite，可在 Worker 启动时显式传入 `--allow-unsafe-sqlite` 逃生参数：
```bash
python -m app.workers.runner --role execution-worker --allow-unsafe-sqlite
```
> **警告**：SQLite 仅支持单进程内调试或本地单测，**绝对不安全（NOT SAFE FOR MULTI-PROCESS）**，严禁在生产或多进程并行环境下使用。

## 2. 日常启动与健康检查
1. 启动服务：`make dev` 或分别启动后端 `uvicorn app.main:app --host 127.0.0.1 --port 8000` 与前端 `npm run dev`。
2. 检查健康状态：访问 `http://127.0.0.1:8000/health` 确认 API、Database、Cache/EventBus 与各后台 Worker 状态。

## 3. V1.2 Worker 进程与安全闸门

分别启动三个进程；它们通过 PostgreSQL 共享状态，不依赖 API 进程内存：

```bash
python -m app.workers.runner --role market-worker
python -m app.workers.runner --role execution-worker
python -m app.workers.runner --role recovery-watchdog
```

- `AUTO_TRADING_ENABLED=false` 时 Market Worker 只刷新行情，Execution Worker 不认领订单任务。
- API `/automation/pause` 与 Kill Switch 均写入数据库；其他进程在下一次循环及实际提交前重新检查。
- Execution Worker 崩溃后，Outbox 租约到期会被另一 Worker 回收。同一 `client_order_id` 保证重放不重复创建订单。
- 可用 `--once --worker-id <id>` 做单次 bounded 验收；`--lease-seconds` 设置认领租约。
- `GET /api/v1/automation/status` 可查看所有进程最近心跳与恢复检查点。

## 4. 常见故障应急预案

| 故障场景 | 系统自动保护行为 | 人工处置步骤 |
| --- | --- | --- |
| 下单超时 (`UNKNOWN` 状态) | 自动冻结同信号重试，标记订单为 `UNKNOWN` | 在 Orders 页面点击“对账恢复 (Reconcile)”，确认交易所是否已成交 |
| 本地与交易所持仓不一致 | 自动锁定该账户新开仓 (`sync_status=MISMATCH`) | 在 Exchanges 或 Positions 页面点击“全量同步对账 (Sync & Reconcile)” |
| 当日亏损触及阈值 | 自动触发日亏损锁定，拒绝一切新开仓 | 复盘 Journal 后，在 Risk Center 确认风险并点击“人工复位 (Resume)” |
| 极端行情或系统异常 | 触发 Kill Switch 紧急熔断 | 在 Risk Center 点击“触发 Kill Switch”禁止新开仓，必要时点击“紧急平仓 (Flatten All)” |
| 机器休眠或重启恢复 | 启动时不沿用旧内存状态 | 重启后先执行账户对账，确认无孤儿挂单或未保护持仓后再开启自动化 |
