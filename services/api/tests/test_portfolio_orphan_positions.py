"""Reconciliation must be bidirectional and must not overstate its evidence.

Review finding P0-4:
  * only local positions were compared, so an exposure that existed on the exchange
    but had no local record was invisible - the worst possible direction to miss;
  * a local `tp_sl_confirmed` boolean was presented as proof that an exchange-native
    protective order still existed.

These tests pin the reverse-direction check and the evidence labels. They do not and
cannot prove real exchange connectivity: the shipped adapters are simulators, and the
labels say so.
"""
from __future__ import annotations

from sqlalchemy import select

from app.models.entities import TradingAccount
from app.portfolio.service import portfolio_service


def test_orphan_exchange_position_is_reported(seeded, monkeypatch):
    db = seeded
    account = db.scalar(select(TradingAccount).where(TradingAccount.exchange == "binance"))
    assert account is not None

    monkeypatch.setattr(
        portfolio_service,
        "_safe_adapter_positions",
        lambda adapter: [
            {"symbol": "DOGE/USDT:USDT", "position_side": "LONG", "qty": "1000"},
        ],
    )

    result = portfolio_service.reconcile_account(db, account.id, force_fix=True)

    assert any("Orphan exchange position" in m for m in result["mismatches"]), result["mismatches"]
    assert result["mismatch_count"] >= 1


def test_zero_qty_remote_position_is_not_an_orphan(seeded, monkeypatch):
    db = seeded
    account = db.scalar(select(TradingAccount).where(TradingAccount.exchange == "binance"))
    assert account is not None

    monkeypatch.setattr(
        portfolio_service,
        "_safe_adapter_positions",
        lambda adapter: [
            {"symbol": "DOGE/USDT:USDT", "position_side": "LONG", "qty": "0"},
        ],
    )

    result = portfolio_service.reconcile_account(db, account.id, force_fix=True)

    assert not any("Orphan exchange position" in m for m in result["mismatches"])


def test_reconciliation_labels_its_evidence(seeded, monkeypatch):
    db = seeded
    account = db.scalar(select(TradingAccount).where(TradingAccount.exchange == "binance"))
    assert account is not None

    monkeypatch.setattr(portfolio_service, "_safe_adapter_positions", lambda adapter: [])

    result = portfolio_service.reconcile_account(db, account.id, force_fix=True)

    assert result["position_evidence"] == "SIMULATED"
    assert result["protective_order_evidence"] == "LOCAL_ONLY"
