# 运维与故障处理手册 (RUNBOOK.md)

## 1. 日常启动与健康检查
1. 启动服务：`make dev` 或分别启动后端 `uvicorn app.main:app --host 127.0.0.1 --port 8000` 与前端 `npm run dev`。
2. 检查健康状态：访问 `http://127.0.0.1:8000/health` 确认 API、Database、Cache/EventBus 与各后台 Worker 状态。

## 2. V1.2 Worker 进程与安全闸门

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

## 3. 常见故障应急预案

| 故障场景 | 系统自动保护行为 | 人工处置步骤 |
| --- | --- | --- |
| 下单超时 (`UNKNOWN` 状态) | 自动冻结同信号重试，标记订单为 `UNKNOWN` | 在 Orders 页面点击“对账恢复 (Reconcile)”，确认交易所是否已成交 |
| 本地与交易所持仓不一致 | 自动锁定该账户新开仓 (`sync_status=MISMATCH`) | 在 Exchanges 或 Positions 页面点击“全量同步对账 (Sync & Reconcile)” |
| 当日亏损触及阈值 | 自动触发日亏损锁定，拒绝一切新开仓 | 复盘 Journal 后，在 Risk Center 确认风险并点击“人工复位 (Resume)” |
| 极端行情或系统异常 | 触发 Kill Switch 紧急熔断 | 在 Risk Center 点击“触发 Kill Switch”禁止新开仓，必要时点击“紧急平仓 (Flatten All)” |
| 机器休眠或重启恢复 | 启动时不沿用旧内存状态 | 重启后先执行账户对账，确认无孤儿挂单或未保护持仓后再开启自动化 |
