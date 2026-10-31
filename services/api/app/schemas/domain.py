from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.precision import to_decimal


class ExchangeConnectionCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    exchange: Literal["binance", "okx"]
    environment: Literal["demo", "testnet", "live"] = "demo"
    api_key: str = Field(..., min_length=4)
    api_secret: str = Field(..., min_length=4)
    passphrase: str | None = None
    region: str = "GLOBAL"
    enable_withdraw_permission_simulation: bool = False
    ip_whitelist_configured: bool = True


class ExchangeConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    exchange: str
    environment: str
    api_key_masked: str
    status: str
    has_withdraw_permission: bool
    ip_whitelist_configured: bool
    permissions_json: dict[str, Any]
    last_sync_at: datetime | None
    last_error: str | None
    created_at: datetime


class StrategyCreateRequest(BaseModel):
    code: str = Field(..., min_length=2, max_length=64)
    name: str = Field(..., min_length=2, max_length=120)
    description: str = ""
    exchange: Literal["binance", "okx"] = "binance"
    market_type: Literal["spot", "usdt_perp"] = "usdt_perp"
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT:USDT", "ETH/USDT:USDT"])
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"] = "15m"
    cooldown_seconds: int = Field(default=900, ge=60)
    max_positions: int = Field(default=2, ge=1, le=10)
    parameters: dict[str, Any] = Field(default_factory=dict)
    entry_conditions: dict[str, Any] = Field(default_factory=dict)
    exit_conditions: dict[str, Any] = Field(default_factory=dict)
    risk_parameters: dict[str, Any] = Field(default_factory=dict)


class StrategyUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    symbols: list[str] | None = None
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"] | None = None
    cooldown_seconds: int | None = None
    max_positions: int | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    entry_conditions: dict[str, Any] = Field(default_factory=dict)
    exit_conditions: dict[str, Any] = Field(default_factory=dict)
    risk_parameters: dict[str, Any] = Field(default_factory=dict)
    change_note: str = Field(default="Updated strategy parameters", min_length=2)


class TradePlanSchema(BaseModel):
    """
    Strict Pydantic schema for AI Agent TradePlan output (Section 3.6).
    Never executed directly; must pass RiskEngine validation.
    """

    exchange: Literal["binance", "okx"]
    market_type: Literal["spot", "usdt_perp"]
    symbol: str = Field(..., min_length=3)
    direction: Literal["LONG", "SHORT", "BUY", "SELL"]
    entry_type: Literal["MARKET", "LIMIT"] = "LIMIT"
    entry_price: Decimal | None = None
    entry_zone: list[Decimal] | None = None
    stop_loss: Decimal
    take_profit: Decimal
    confidence: Decimal = Field(..., ge=Decimal("0"), le=Decimal("1"))
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"]
    thesis: str = Field(..., min_length=10)
    invalidation_conditions: list[str] = Field(..., min_length=1)
    expires_at: datetime
    strategy_version: str = Field(..., min_length=1)

    @field_validator("entry_price", "stop_loss", "take_profit", "confidence", mode="before")
    @classmethod
    def _coerce_decimal(cls, val: Any) -> Decimal | None:
        if val is None:
            return None
        return to_decimal(val)

    @field_validator("entry_zone", mode="before")
    @classmethod
    def _coerce_zone(cls, val: Any) -> list[Decimal] | None:
        if val is None:
            return None
        if not isinstance(val, (list, tuple)) or len(val) != 2:
            raise ValueError("entry_zone must be a 2-element list [low, high]")
        low = to_decimal(val[0])
        high = to_decimal(val[1])
        if low <= Decimal("0") or high < low:
            raise ValueError("entry_zone must have 0 < low <= high")
        return [low, high]

    @field_validator("expires_at", mode="before")
    @classmethod
    def _coerce_utc(cls, val: Any) -> datetime:
        if isinstance(val, str):
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        elif isinstance(val, datetime):
            dt = val
        else:
            raise ValueError("Invalid expires_at datetime")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_plan_semantics(self) -> "TradePlanSchema":
        if self.entry_price is None and not self.entry_zone:
            raise ValueError("Either entry_price or entry_zone must be provided")
        if self.entry_price is None and self.entry_zone:
            self.entry_price = (self.entry_zone[0] + self.entry_zone[1]) / Decimal("2")

        assert self.entry_price is not None
        if self.entry_price <= Decimal("0") or self.stop_loss <= Decimal("0") or self.take_profit <= Decimal("0"):
            raise ValueError("Prices must be strictly positive")

        if self.market_type == "spot" and self.direction in ("SHORT",):
            raise ValueError("Spot market does not support SHORT opening positions in V1.0")

        is_long = self.direction in ("LONG", "BUY")
        if is_long:
            if not (self.stop_loss < self.entry_price < self.take_profit):
                raise ValueError(
                    f"LONG/BUY plan requires stop_loss ({self.stop_loss}) < entry_price ({self.entry_price}) < take_profit ({self.take_profit})"
                )
        else:
            if not (self.take_profit < self.entry_price < self.stop_loss):
                raise ValueError(
                    f"SHORT/SELL plan requires take_profit ({self.take_profit}) < entry_price ({self.entry_price}) < stop_loss ({self.stop_loss})"
                )
        return self


class RiskLimitsSchema(BaseModel):
    max_risk_per_trade_pct: Decimal = Field(default=Decimal("0.0025"), gt=Decimal("0"), le=Decimal("0.05"))
    max_daily_loss_pct: Decimal = Field(default=Decimal("0.0100"), gt=Decimal("0"), le=Decimal("0.20"))
    max_total_exposure_pct: Decimal = Field(default=Decimal("0.3000"), gt=Decimal("0"), le=Decimal("2.00"))
    max_symbol_exposure_pct: Decimal = Field(default=Decimal("0.1500"), gt=Decimal("0"), le=Decimal("1.00"))
    max_open_positions: int = Field(default=3, ge=1, le=20)
    max_leverage: Decimal = Field(default=Decimal("2"), ge=Decimal("1"), le=Decimal("10"))
    max_slippage_bps: Decimal = Field(default=Decimal("25"), ge=Decimal("1"), le=Decimal("200"))
    max_spread_bps: Decimal = Field(default=Decimal("20"), ge=Decimal("1"), le=Decimal("200"))
    max_data_stale_seconds: int = Field(default=30, ge=5, le=600)
    max_consecutive_api_failures: int = Field(default=3, ge=1, le=20)
    max_consecutive_losses: int = Field(default=3, ge=1, le=20)
    max_daily_trades: int = Field(default=20, ge=1, le=200)
    account_paused: bool = False


class OrderSubmitRequest(BaseModel):
    account_id: str | None = None
    trade_plan: TradePlanSchema
    signal_id: str | None = None
    decision_id: str | None = None
    client_order_id: str | None = None
    simulate_timeout: bool = False
    simulate_partial_fill: bool = False


class KillSwitchRequest(BaseModel):
    scope: str = "global"
    reason: str = Field(default="Manual operator emergency stop", min_length=3)


class ResumeTradingRequest(BaseModel):
    scope: str = "global"
    confirmation_code: str = Field(..., description="Must be 'CONFIRM_RESUME'")
    reason: str = "Operator verified account reconciliation and risk status"


class ResetDailyLocksRequest(BaseModel):
    account_id: str | None = None
    confirmation_code: str = Field(..., description="Must be 'CONFIRM_RESET'")
    reason: str = Field(
        default="Operator acknowledged the loss streak and reset daily counters",
        min_length=5,
    )


class AutomationControlRequest(BaseModel):
    environment: Literal["demo", "testnet", "live"] = "demo"
    live_confirmation_phrase: str | None = None
    strategy_id: str | None = None


class BacktestCreateRequest(BaseModel):
    strategy_id: str | None = None
    strategy_code: str = "trend_following_v1"
    exchange: Literal["binance", "okx"] = "binance"
    market_type: Literal["spot", "usdt_perp"] = "usdt_perp"
    symbol: str = "BTC/USDT:USDT"
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"] = "15m"
    initial_equity: Decimal = Decimal("10000")
    fee_rate: Decimal = Decimal("0.0005")
    slippage_bps: Decimal = Decimal("5")
    funding_rate_8h: Decimal = Decimal("0.0001")
    out_of_sample_ratio: Decimal = Decimal("0.30")
    bars_count: int = Field(default=160, ge=60, le=1000)
