from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.core.config import settings
from app.exchanges.base import BaseSimulatedExchangeAdapter


class OKXAdapter(BaseSimulatedExchangeAdapter):
    """
    OKX V5 Unified Account Spot + USDT Perpetual Swap Adapter (Section 7.3).
    Supports demo (x-simulated-trading: 1), testnet, and live environments.
    Note realistic contract multipliers on OKX USDT-M Swaps:
      - BTC/USDT:USDT (BTC-USDT-SWAP): contract_size = Decimal("0.01") BTC per contract
      - ETH/USDT:USDT (ETH-USDT-SWAP): contract_size = Decimal("0.1") ETH per contract
      - SOL/USDT:USDT (SOL-USDT-SWAP): contract_size = Decimal("1") SOL per contract
    """

    exchange: str = "okx"
    default_fee_rate_spot: Decimal = Decimal("0.0008")
    default_fee_rate_perp: Decimal = Decimal("0.0005")

    def _build_default_instruments(self) -> list[dict[str, Any]]:
        return [
            {
                "exchange": "okx",
                "market_type": "spot",
                "native_symbol": "BTC-USDT",
                "canonical_symbol": "BTC/USDT",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "settle_asset": "USDT",
                "tick_size": Decimal("0.1"),
                "lot_size": Decimal("0.0001"),
                "min_qty": Decimal("0.0001"),
                "min_notional": Decimal("1.0"),
                "contract_size": Decimal("1"),
                "max_leverage": Decimal("1"),
                "status": "TRADING",
            },
            {
                "exchange": "okx",
                "market_type": "spot",
                "native_symbol": "ETH-USDT",
                "canonical_symbol": "ETH/USDT",
                "base_asset": "ETH",
                "quote_asset": "USDT",
                "settle_asset": "USDT",
                "tick_size": Decimal("0.01"),
                "lot_size": Decimal("0.001"),
                "min_qty": Decimal("0.001"),
                "min_notional": Decimal("1.0"),
                "contract_size": Decimal("1"),
                "max_leverage": Decimal("1"),
                "status": "TRADING",
            },
            {
                "exchange": "okx",
                "market_type": "spot",
                "native_symbol": "SOL-USDT",
                "canonical_symbol": "SOL/USDT",
                "base_asset": "SOL",
                "quote_asset": "USDT",
                "settle_asset": "USDT",
                "tick_size": Decimal("0.01"),
                "lot_size": Decimal("0.01"),
                "min_qty": Decimal("0.01"),
                "min_notional": Decimal("1.0"),
                "contract_size": Decimal("1"),
                "max_leverage": Decimal("1"),
                "status": "TRADING",
            },
            {
                "exchange": "okx",
                "market_type": "usdt_perp",
                "native_symbol": "BTC-USDT-SWAP",
                "canonical_symbol": "BTC/USDT:USDT",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "settle_asset": "USDT",
                "tick_size": Decimal("0.1"),
                "lot_size": Decimal("0.01"),
                "min_qty": Decimal("0.01"),
                "min_notional": Decimal("5.0"),
                "contract_size": Decimal("0.01"),
                "max_leverage": Decimal("20"),
                "status": "TRADING",
            },
            {
                "exchange": "okx",
                "market_type": "usdt_perp",
                "native_symbol": "ETH-USDT-SWAP",
                "canonical_symbol": "ETH/USDT:USDT",
                "base_asset": "ETH",
                "quote_asset": "USDT",
                "settle_asset": "USDT",
                "tick_size": Decimal("0.01"),
                "lot_size": Decimal("0.01"),
                "min_qty": Decimal("0.01"),
                "min_notional": Decimal("5.0"),
                "contract_size": Decimal("0.1"),
                "max_leverage": Decimal("20"),
                "status": "TRADING",
            },
            {
                "exchange": "okx",
                "market_type": "usdt_perp",
                "native_symbol": "SOL-USDT-SWAP",
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
                "rest_url": settings.OKX_REST_URL,
                "simulated_trading_flag": 1 if self.environment in ("demo", "testnet") else 0,
                "account_level": "SINGLE_CURRENCY_OR_MULTI_CURRENCY_MARGIN",
                "requires_passphrase": True,
            }
        )
        return caps
