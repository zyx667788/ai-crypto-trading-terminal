from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
from pydantic import ValidationError

from app.core.precision import (
    calc_position_sizing,
    calc_spread_bps,
    round_price_to_tick,
    round_qty_to_lot,
    utc_now,
)
from app.core.security import mask_secret, redact_sensitive_data, secret_vault
from app.schemas.domain import TradePlanSchema


def test_decimal_rounding_tick_and_lot():
    assert round_price_to_tick(Decimal("68421.378"), Decimal("0.1")) == Decimal("68421.4")
    assert round_price_to_tick(Decimal("3452.124"), Decimal("0.05")) == Decimal("3452.10")
    assert round_qty_to_lot(Decimal("0.12399"), Decimal("0.001")) == Decimal("0.123")
    assert round_qty_to_lot(Decimal("15.99"), Decimal("1")) == Decimal("15")


def test_spread_bps_calculation():
    spread = calc_spread_bps(Decimal("60000"), Decimal("60030"))
    # (30 / 60015) * 10000 = 5.00 bps
    assert spread == Decimal("5.00")


def test_position_sizing_spot_vs_perp_contract_size():
    # Spot BTC/USDT: contract_size = 1
    spot_sizing = calc_position_sizing(
        account_equity=Decimal("10000"),
        risk_rate=Decimal("0.0025"),  # $25 risk budget
        entry_price=Decimal("50000"),
        stop_price=Decimal("49000"),  # $1000 stop distance
        market_type="spot",
        lot_size=Decimal("0.0001"),
        min_qty=Decimal("0.0001"),
        min_notional=Decimal("5.0"),
        contract_size=Decimal("1"),
        fee_rate=Decimal("0.0005"),
        slippage_bps=Decimal("5"),
        leverage=Decimal("1"),
    )
    assert isinstance(spot_sizing["qty"], Decimal)
    assert spot_sizing["risk_budget"] == Decimal("25.0000")
    assert spot_sizing["qty"] > Decimal("0")
    assert spot_sizing["max_planned_loss"] <= Decimal("25.0000")

    # OKX USDT Perp BTC/USDT:USDT where 1 contract = 0.01 BTC
    okx_perp_sizing = calc_position_sizing(
        account_equity=Decimal("10000"),
        risk_rate=Decimal("0.0025"),  # $25 risk budget
        entry_price=Decimal("50000"),
        stop_price=Decimal("49000"),
        market_type="usdt_perp",
        lot_size=Decimal("1"),        # 1 contract step
        min_qty=Decimal("1"),
        min_notional=Decimal("5.0"),
        contract_size=Decimal("0.01"),
        fee_rate=Decimal("0.0005"),
        slippage_bps=Decimal("5"),
        leverage=Decimal("2"),
    )
    # Raw base qty is ~0.02325 BTC -> ~2.325 contracts -> rounded down to 2 contracts (0.02 BTC)
    assert okx_perp_sizing["qty"] == Decimal("2")
    assert okx_perp_sizing["base_qty"] == Decimal("0.02")
    assert okx_perp_sizing["notional"] == Decimal("1000.0000")
    assert okx_perp_sizing["margin_required"] == Decimal("500.0000")


def test_secret_vault_and_redaction():
    raw_secret = "super_secret_binance_key_1234567890"
    encrypted = secret_vault.encrypt(raw_secret)
    assert encrypted is not None
    assert encrypted != raw_secret
    assert secret_vault.decrypt(encrypted) == raw_secret

    masked = mask_secret(raw_secret)
    assert masked == "supe****7890"
    assert raw_secret not in masked

    payload = {
        "exchange": "okx",
        "api_key": "okx_live_key_9999",
        "api_key_masked": "okx_****9999",
        "nested": {
            "api_secret": "dont_leak_me",
            "passphrase": "my_passphrase",
            "symbol": "BTC/USDT:USDT",
        },
    }
    scrubbed = redact_sensitive_data(payload)
    assert scrubbed["api_key"] == "[REDACTED]"
    assert scrubbed["api_key_masked"] == "okx_****9999"
    assert scrubbed["nested"]["api_secret"] == "[REDACTED]"
    assert scrubbed["nested"]["passphrase"] == "[REDACTED]"
    assert scrubbed["nested"]["symbol"] == "BTC/USDT:USDT"


def test_trade_plan_schema_validation():
    future_exp = (utc_now() + timedelta(minutes=30)).isoformat()
    valid_plan = TradePlanSchema.model_validate(
        {
            "exchange": "binance",
            "market_type": "usdt_perp",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_type": "LIMIT",
            "entry_price": "68000.00",
            "stop_loss": "67200.00",
            "take_profit": "69800.00",
            "confidence": "0.82",
            "timeframe": "15m",
            "thesis": "EMA 21 > EMA 50 with strong volume breakout and positive orderbook imbalance.",
            "invalidation_conditions": ["Close below EMA 50 (67350)", "Funding rate spikes above 0.05%"],
            "expires_at": future_exp,
            "strategy_version": "trend_following_v1:1",
        }
    )
    assert valid_plan.entry_price == Decimal("68000.00")
    assert valid_plan.expires_at.tzinfo == timezone.utc

    # Invalid stop loss direction for LONG (stop_loss > entry_price)
    with pytest.raises(ValidationError):
        TradePlanSchema.model_validate(
            {
                "exchange": "binance",
                "market_type": "usdt_perp",
                "symbol": "BTC/USDT:USDT",
                "direction": "LONG",
                "entry_type": "LIMIT",
                "entry_price": "68000.00",
                "stop_loss": "68500.00",
                "take_profit": "69800.00",
                "confidence": "0.80",
                "timeframe": "15m",
                "thesis": "Invalid stop loss test case",
                "invalidation_conditions": ["Price drops"],
                "expires_at": future_exp,
                "strategy_version": "v1",
            }
        )

    # Spot market cannot open SHORT in V1.0
    with pytest.raises(ValidationError):
        TradePlanSchema.model_validate(
            {
                "exchange": "okx",
                "market_type": "spot",
                "symbol": "BTC/USDT",
                "direction": "SHORT",
                "entry_type": "LIMIT",
                "entry_price": "68000.00",
                "stop_loss": "69000.00",
                "take_profit": "66000.00",
                "confidence": "0.75",
                "timeframe": "15m",
                "thesis": "Spot short should be rejected",
                "invalidation_conditions": ["Price rises"],
                "expires_at": future_exp,
                "strategy_version": "v1",
            }
        )
