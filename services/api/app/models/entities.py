from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.precision import utc_now
from app.db.session import Base
from app.db.types import DecimalNumeric, UTCDateTime


def new_uuid() -> str:
    return str(uuid.uuid4())


class ExchangeConnection(Base):
    __tablename__ = "exchange_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    owner_id: Mapped[str] = mapped_column(String(64), default="local_admin", index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # binance | okx
    environment: Mapped[str] = mapped_column(String(32), nullable=False, default="demo")  # demo | testnet | live
    api_key_masked: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_key_ref: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_passphrase_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="CONNECTED")
    has_withdraw_permission: Mapped[bool] = mapped_column(Boolean, default=False)
    ip_whitelist_configured: Mapped[bool] = mapped_column(Boolean, default=False)
    permissions_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_sync_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class TradingAccount(Base):
    __tablename__ = "trading_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    owner_id: Mapped[str] = mapped_column(String(64), default="local_admin", index=True)
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("exchange_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False, default="demo")
    account_type: Mapped[str] = mapped_column(String(32), nullable=False, default="unified")
    region: Mapped[str] = mapped_column(String(32), nullable=False, default="GLOBAL")
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    trading_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    lock_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_status: Mapped[str] = mapped_column(String(32), default="SYNCED")
    last_reconciled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("exchange", "market_type", "native_symbol", name="uq_instrument_exchange_market_symbol"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    market_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # spot | usdt_perp
    native_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    base_asset: Mapped[str] = mapped_column(String(32), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(32), nullable=False)
    settle_asset: Mapped[str] = mapped_column(String(32), nullable=False, default="USDT")
    tick_size: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0.01"))
    lot_size: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0.001"))
    min_qty: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0.001"))
    min_notional: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("5.0"))
    contract_size: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("1.0"))
    max_leverage: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("20.0"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="TRADING")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class MarketCandle(Base):
    __tablename__ = "market_candles"
    __table_args__ = (
        UniqueConstraint(
            "exchange",
            "market_type",
            "symbol",
            "timeframe",
            "open_time",
            name="uq_market_candle_series",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    instrument_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("instruments.id"), nullable=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    market_type: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    open_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    close_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    open: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    high: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    low: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    close: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    volume: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    quote_volume: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source_exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    instrument_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("instruments.id"), nullable=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    market_type: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    last: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    bid: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    ask: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    spread_bps: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    volume_24h: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    quote_volume_24h: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    change_24h_pct: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    mark_price: Mapped[Decimal | None] = mapped_column(DecimalNumeric, nullable=True)
    index_price: Mapped[Decimal | None] = mapped_column(DecimalNumeric, nullable=True)
    funding_rate: Mapped[Decimal | None] = mapped_column(DecimalNumeric, nullable=True)
    next_funding_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    orderbook_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    indicators_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_stale: Mapped[bool] = mapped_column(Boolean, default=False)


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    owner_id: Mapped[str] = mapped_column(String(64), default="local_admin")
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    exchange: Mapped[str] = mapped_column(String(32), nullable=False, default="binance")
    market_type: Mapped[str] = mapped_column(String(32), nullable=False, default="usdt_perp")
    symbols_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False, default="15m")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")  # ACTIVE | PAUSED
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=900)
    max_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (
        UniqueConstraint("strategy_id", "version", name="uq_strategy_version_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    strategy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    entry_conditions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    exit_conditions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    risk_parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    change_note: Mapped[str] = mapped_column(Text, default="Initial version")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class StrategyRun(Base):
    __tablename__ = "strategy_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    strategy_id: Mapped[str] = mapped_column(String(36), ForeignKey("strategies.id"), nullable=False, index=True)
    strategy_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("strategy_versions.id"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="COMPLETED")
    signals_generated: Mapped[int] = mapped_column(Integer, default=0)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # market | scanner | strategy | risk | journal
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="rule_assisted_mock")
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1.0")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="SUCCEEDED")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    input_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, index=True)


class AIDecision(Base):
    __tablename__ = "ai_decisions"
    __table_args__ = (
        Index("ix_ai_decisions_created_strategy_ver", "created_at", "strategy_version_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_runs.id"), nullable=False, index=True)
    strategy_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    market_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    market_type: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0.75"))
    output_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    risk_explanation: Mapped[str] = mapped_column(Text, default="")
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0")
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    validation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class TradeSignal(Base):
    __tablename__ = "trade_signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    strategy_run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("strategy_runs.id"), nullable=True)
    strategy_version_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    decision_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("ai_decisions.id"), nullable=True)
    instrument_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("instruments.id"), nullable=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    market_type: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False, default="15m")
    direction: Mapped[str] = mapped_column(String(16), nullable=False)  # LONG | SHORT | BUY | SELL
    trigger_reason: Mapped[str] = mapped_column(Text, nullable=False)
    indicators_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    score: Mapped[Decimal] = mapped_column(DecimalNumeric, default=Decimal("75.0"))
    dedup_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")  # ACTIVE | CONSUMED | EXPIRED | REJECTED
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class RiskProfile(Base):
    __tablename__ = "risk_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    limits_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Operator acknowledgement watermark: daily-loss / loss-streak counters only consider
    # activity AFTER this instant. History stays append-only; nothing is deleted.
    lock_reset_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    lock_reset_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    signal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    rule_code: Mapped[str] = mapped_column(String(64), nullable=False, default="RISK_CHECK")
    severity: Mapped[str] = mapped_column(String(32), nullable=False, default="WARNING")  # INFO | WARNING | CRITICAL
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, index=True)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("account_id", "client_order_id", name="uq_orders_account_client_order_id"),
        Index("ix_orders_account_status_created", "account_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    signal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    strategy_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False, default="demo")
    market_type: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    order_type: Mapped[str] = mapped_column(String(32), nullable=False, default="LIMIT")
    side: Mapped[str] = mapped_column(String(16), nullable=False)  # BUY | SELL
    position_side: Mapped[str] = mapped_column(String(16), nullable=False, default="NET")  # LONG | SHORT | NET
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="CREATED")
    qty: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    filled_qty: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    price: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    avg_fill_price: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    stop_loss: Mapped[Decimal | None] = mapped_column(DecimalNumeric, nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(DecimalNumeric, nullable=True)
    tp_sl_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    protection_order_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class Fill(Base):
    __tablename__ = "fills"
    __table_args__ = (
        UniqueConstraint("exchange", "exchange_trade_id", name="uq_fills_exchange_trade_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    order_id: Mapped[str] = mapped_column(String(36), ForeignKey("orders.id"), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    market_type: Mapped[str] = mapped_column(String(32), nullable=False, default="usdt_perp")
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange_trade_id: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    price: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    qty: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    fee: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    fee_currency: Mapped[str] = mapped_column(String(16), nullable=False, default="USDT")
    realized_pnl: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_positions_account_instrument_side", "account_id", "instrument_id", "position_side"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    instrument_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False, default="demo")
    market_type: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    position_side: Mapped[str] = mapped_column(String(16), nullable=False, default="LONG")  # LONG | SHORT | SPOT
    qty: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    mark_price: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    liquidation_price: Mapped[Decimal | None] = mapped_column(DecimalNumeric, nullable=True)
    leverage: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("1"))
    margin_used: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    notional_value: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    unrealized_pnl: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    stop_loss: Mapped[Decimal | None] = mapped_column(DecimalNumeric, nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(DecimalNumeric, nullable=True)
    tp_sl_confirmed: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="OPEN")  # OPEN | CLOSED
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False, default="binance")
    asset: Mapped[str] = mapped_column(String(32), nullable=False)
    total: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    available: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    locked: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    usd_value: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, index=True)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False, default="all")
    equity: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    available_balance: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    margin_balance: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    notional_exposure: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    unrealized_pnl: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    fees_paid: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    funding_pnl: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    net_transfers: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False, default=Decimal("0"))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, index=True)


class Transfer(Base):
    __tablename__ = "transfers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    transfer_type: Mapped[str] = mapped_column(String(32), nullable=False)  # DEPOSIT | WITHDRAWAL | INTERNAL
    asset: Mapped[str] = mapped_column(String(32), nullable=False, default="USDT")
    amount: Mapped[Decimal] = mapped_column(DecimalNumeric, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_created_resource", "created_at", "resource"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="local_admin")
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource: Mapped[str] = mapped_column(String(64), nullable=False)
    before_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class SystemEvent(Base):
    __tablename__ = "system_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    component: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, default="INFO")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, index=True)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    strategy_version_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    dataset: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    equity_curve_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    trades_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="COMPLETED")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class KillSwitchState(Base):
    __tablename__ = "kill_switch_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    scope: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)  # global or account:{id}
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    order_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    signal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    strategy_id: Mapped[str] = mapped_column(String(36), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    market_type: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    ai_model_version: Mapped[str] = mapped_column(String(64), default="deepseek-reasoner:v1.0")
    ai_output_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    risk_check_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    order_request_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fills_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    fees_paid: Mapped[Decimal] = mapped_column(DecimalNumeric, default=Decimal("0"))
    funding_pnl: Mapped[Decimal] = mapped_column(DecimalNumeric, default=Decimal("0"))
    close_reason: Mapped[str] = mapped_column(String(64), default="OPEN_OR_ACTIVE")
    net_realized_pnl: Mapped[Decimal] = mapped_column(DecimalNumeric, default=Decimal("0"))
    review_summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, index=True)
