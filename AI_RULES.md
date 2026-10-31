# Project Rules (AI_RULES.md)

1. Read `README.md` and `docs/ARCHITECTURE.md` before coding.
2. Follow the existing module boundaries (`exchanges`, `market_data`, `scanner`, `strategies`, `ai`, `risk`, `execution`, `portfolio`, `journal`, `workers`).
3. Never expose API secrets, passphrases, or signing keys to frontend code, browser storage, logs, or LLM prompts.
4. Never enable live trading by default (`LIVE_TRADING_ENABLED=false` by default; requires explicit human confirmation and pre-flight verification).
5. All orders must pass the deterministic `RiskEngine` before reaching `ExecutionEngine`. AI outputs (`TradePlan`) are never executed directly.
6. Never retry an order blindly after a timeout. Orders with ambiguous outcomes transition to `UNKNOWN` and require exchange query + reconciliation first.
7. Use `Decimal` for all money, price, quantity, leverage, margin, and fee calculations. Never use `float` for financial math.
8. Use UTC (`TIMESTAMPTZ` / ISO-8601 UTC) for all timestamps.
9. Every database schema change requires an Alembic migration.
10. Every feature requires unit and integration tests.
11. Never remove or weaken a risk rule to make tests pass.
12. Use exchange Demo/Testnet environments for integration testing and default paper execution.
13. Do not invent exchange API fields; verify against official Binance and OKX V5 documentation.
14. Keep secrets out of logs, database snapshots, exception traces, and error messages.
15. Update relevant documentation in `docs/` when API contracts or database schemas change.
