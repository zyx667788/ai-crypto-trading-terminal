from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.core.config import settings
from app.exchanges.base import BaseSimulatedExchangeAdapter


class BinanceAdapter(BaseSimulatedExchangeAdapter):
    """
    Binance Spot + USDⓈ-M Perpetual Futures Adapter (Section 7.2).
    Supports demo, testnet, and live environments.
    In Binance USDⓈ-M Perpetual Futures, contract_size is Decimal("1") (base asset units).
    """

    exchange: str = "binance"
    default_fee_rate_spot: Decimal = Decimal("0.0010")
    default_fee_rate_perp: Decimal = Decimal("0.0004")

    def _build_default_instruments(self) -> list[dict[str, Any]]:
        return [
            {
                "exchange": "binance",
                "market_type": "spot",
                "native_symbol": "BTCUSDT",
                "canonical_symbol": "BTC/USDT",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "settle_asset": "USDT",
                "tick_size": Decimal("0.01"),
                "lot_size": Decimal("0.0001"),
                "min_qty": Decimal("0.0001"),
                "min_notional": Decimal("5.0"),
                "contract_size": Decimal("1"),
                "max_leverage": Decimal("1"),
                "status": "TRADING",
            },
            {
                "exchange": "binance",
                "market_type": "spot",
                "native_symbol": "ETHUSDT",
                "canonical_symbol": "ETH/USDT",
                "base_asset": "ETH",
                "quote_asset": "USDT",
                "settle_asset": "USDT",
                "tick_size": Decimal("0.01"),
                "lot_size": Decimal("0.001"),
                "min_qty": Decimal("0.001"),
                "min_notional": Decimal("5.0"),
                "contract_size": Decimal("1"),
                "max_leverage": Decimal("1"),
                "status": "TRADING",
            },
            {
                "exchange": "binance",
                "market_type": "spot",
                "native_symbol": "SOLUSDT",
                "canonical_symbol": "SOL/USDT",
                "base_asset": "SOL",
                "quote_asset": "USDT",
                "settle_asset": "USDT",
                "tick_size": Decimal("0.01"),
                "lot_size": Decimal("0.01"),
                "min_qty": Decimal("0.01"),
                "min_notional": Decimal("5.0"),
                "contract_size": Decimal("1"),
                "max_leverage": Decimal("1"),
                "status": "TRADING",
            },
            {
                "exchange": "binance",
                "market_type": "usdt_perp",
                "native_symbol": "BTCUSDT",
                "canonical_symbol": "BTC/USDT:USDT",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "settle_asset": "USDT",
                "tick_size": Decimal("0.10"),
                "lot_size": Decimal("0.001"),
                "min_qty": Decimal("0.001"),
                "min_notional": Decimal("5.0"),
                "contract_size": Decimal("1"),
                "max_leverage": Decimal("20"),
                "status": "TRADING",
            },
            {
                "exchange": "binance",
                "market_type": "usdt_perp",
                "native_symbol": "ETHUSDT",
                "canonical_symbol": "ETH/USDT:USDT",
                "base_asset": "ETH",
                "quote_asset": "USDT",
                "settle_asset": "USDT",
                "tick_size": Decimal("0.01"),
                "lot_size": Decimal("0.001"),
                "min_qty": Decimal("0.001"),
                "min_notional": Decimal("5.0"),
                "contract_size": Decimal("1"),
                "max_leverage": Decimal("20"),
                "status": "TRADING",
            },
            {
                "exchange": "binance",
                "market_type": "usdt_perp",
                "native_symbol": "SOLUSDT",
                "canonical_symbol": "SOL/USDT:USDT",
                "base_asset": "SOL",
                "quote_asset": "USDT",
                "settle_asset": "USDT",
                "tick_size": Decimal("0.01"),
                "lot_size": Decimal("0.1"),
                "min_qty": Decimal("0.1"),
                "min_notional": Decimal("5.0"),
                "contract_size": Decimal("1"),
                "max_leverage": Decimal("20"),
                "status": "TRADING",
            },
        ]

    def get_capabilities(self) -> dict[str, Any]:
        caps = super().get_capabilities()
        caps.update(
            {
                "spot_rest_url": settings.BINANCE_SPOT_REST_URL,
                "futures_rest_url": settings.BINANCE_FUTURES_REST_URL,
                "position_mode": "ONE_WAY_OR_HEDGE",
                "margin_mode": "ISOLATED_OR_CROSS",
            }
        )
        return caps
