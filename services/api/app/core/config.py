from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[4]
DATA_DIR = ROOT_DIR / "data"
LOGS_DIR = ROOT_DIR / "logs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_NAME: str = "AI Crypto Trading Terminal"
    APP_ENV: str = "development"
    APP_HOST: str = "127.0.0.1"
    APP_PORT: int = 8000
    WEB_URL: str = "http://localhost:3000"

    DATABASE_URL: str = f"sqlite:///{(DATA_DIR / 'terminal.db').as_posix()}"
    REDIS_URL: str = "redis://127.0.0.1:6379/0"

    KEYCHAIN_SERVICE_NAME: str = "ai-crypto-trading-terminal"
    KEYCHAIN_ACCOUNT_NAME: str = "master-encryption-key"

    DEFAULT_TRADING_ENVIRONMENT: Literal["demo", "testnet", "live"] = "demo"
    LIVE_TRADING_ENABLED: bool = False
    LIVE_MAX_ORDER_NOTIONAL_USDT: Decimal = Decimal("100.00")
    AUTO_TRADING_ENABLED: bool = False
    RECOVERY_CHECKPOINT_TTL_SECONDS: int = 90

    # ------------------------------------------------- runtime hardening (V1.2.1)
    # Multi-process supervision (API + market worker + execution worker + watchdog)
    # requires a database with real cross-process locking. SQLite cannot provide it,
    # so the trading runtime refuses to boot on SQLite unless explicitly downgraded.
    REQUIRE_DB_DIALECT: Literal["postgresql", "sqlite", "any"] = "postgresql"
    ENFORCE_DB_DIALECT_IN_PRODUCTION: bool = True
    DB_POLICY_ALLOW_SQLITE_IN_PRODUCTION: bool = False

    # A worker heartbeat older than this is reported as STALE and marks readiness red.
    WORKER_HEARTBEAT_TTL_SECONDS: int = 90

    # Local admin API protection. An empty token disables header auth but keeps
    # Origin/Host enforcement; set it to require an explicit X-Admin-Token header.
    ADMIN_API_TOKEN: str = ""
    ENFORCE_ADMIN_ORIGIN: bool = True
    ADMIN_ALLOWED_ORIGINS: str = (
        "http://localhost:3000,http://127.0.0.1:3000,"
        "http://localhost:8000,http://127.0.0.1:8000"
    )

    # Conservative Default Risk Limits (Section 3.7)
    DEFAULT_MAX_RISK_PER_TRADE_PCT: Decimal = Decimal("0.0025")   # 0.25% equity
    DEFAULT_MAX_DAILY_LOSS_PCT: Decimal = Decimal("0.0100")       # 1.00% equity
    DEFAULT_MAX_TOTAL_EXPOSURE_PCT: Decimal = Decimal("0.3000")   # 30% equity
    DEFAULT_MAX_SYMBOL_EXPOSURE_PCT: Decimal = Decimal("0.1500")  # 15% equity
    DEFAULT_MAX_OPEN_POSITIONS: int = 3
    DEFAULT_MAX_LEVERAGE: Decimal = Decimal("2")
    DEFAULT_MAX_SLIPPAGE_BPS: Decimal = Decimal("25")
    DEFAULT_MAX_SPREAD_BPS: Decimal = Decimal("20")
    DEFAULT_MAX_DATA_STALE_SECONDS: int = 30
    DEFAULT_MAX_CONSECUTIVE_API_FAILURES: int = 3
    DEFAULT_MAX_CONSECUTIVE_LOSSES: int = 3
    DEFAULT_MAX_DAILY_TRADES: int = 20

    # AI Provider Configuration
    AI_PROVIDER: str = "rule_assisted_mock"
    AI_MODEL: str = "deepseek-reasoner"
    AI_API_KEY: str = ""
    AI_BASE_URL: str = "https://api.deepseek.com/v1"
    AI_TIMEOUT_SECONDS: int = 15
    AI_MAX_RETRIES: int = 1

    # Exchange Endpoints
    BINANCE_SPOT_REST_URL: str = "https://testnet.binance.vision"
    BINANCE_FUTURES_REST_URL: str = "https://testnet.binancefuture.com"
    OKX_REST_URL: str = "https://www.okx.com"
    OKX_SIMULATED_TRADING: int = 1

    @property
    def resolved_database_url(self) -> str:
        """
        Resolve a relative SQLite path against the repository root.

        A relative `sqlite:///` URL would otherwise resolve against the process CWD,
        which differs between `uvicorn app.main:app`, pytest and the CLI scripts.
        """
        prefix = "sqlite:///"
        if not self.DATABASE_URL.startswith(prefix):
            return self.DATABASE_URL

        raw_path = self.DATABASE_URL[len(prefix):]
        if raw_path.startswith("/") or (len(raw_path) > 1 and raw_path[1] == ":"):
            return self.DATABASE_URL  # already absolute (POSIX or Windows drive)

        db_file = (ROOT_DIR / raw_path.lstrip("./")).resolve()
        db_file.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{db_file.as_posix()}"


settings = Settings()
