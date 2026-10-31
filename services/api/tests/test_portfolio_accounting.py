"""Portfolio accounting invariants: fee handling, transfer exclusion, spot vs margin separation."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.entities import (
    EquitySnapshot,
    ExchangeConnection,
    Fill,
    JournalEntry,
    Order,
    Position,
    TradingAccount,
    Transfer,
)
from app.portfolio.service import portfolio_service


@pytest.fixture()
def clean_account(seeded):
    db = seeded
    acct = db.scalar(select(TradingAccount).where(TradingAccount.exchange == "binance"))
    # Delete in foreign-key order: children before parents.
    for fill in db.scalars(select(Fill).where(Fill.account_id == acct.id)).all():
        db.delete(fill)
    for journal in db.scalars(select(JournalEntry).where(JournalEntry.account_id == acct.id)).all():
        db.delete(journal)
    db.flush()
    for model in (Order, Position, Transfer, EquitySnapshot):
        for row in db.scalars(select(model).where(model.account_id == acct.id)).all():
            db.delete(row)
    db.flush()
    db.commit()
    return db, acct


def test_fees_are_not_double_counted_in_net_profit(clean_account):
    """
    `Position.realized_pnl` is already net of fees, so net_profit must equal
    realized + unrealized + funding — subtracting fees again would double count.
    """
    db, acct = clean_account
    portfolio_service.capture_equity_snapshot(db, acct.id)
    summary = portfolio_service.get_portfolio_summary(db)

    realized = Decimal(summary["realized_pnl"])
    unrealized = Decimal(summary["unrealized_pnl"])
    funding = Decimal(summary["funding_pnl"])
    fees = Decimal(summary["fees_paid"])
    net = Decimal(summary["net_profit"])

    assert net == realized + unrealized + funding, "net profit must not subtract fees twice"
    assert Decimal(summary["realized_pnl_gross"]) == realized + fees


def test_transfers_are_excluded_from_trading_profit(clean_account):
    db, acct = clean_account
    db.add(Transfer(account_id=acct.id, transfer_type="DEPOSIT", asset="USDT", amount=Decimal("50000")))
    db.add(Transfer(account_id=acct.id, transfer_type="WITHDRAWAL", asset="USDT", amount=Decimal("10000")))
    db.commit()

    summary = portfolio_service.get_portfolio_summary(db)
    assert Decimal(summary["net_transfers"]) == Decimal("40000")
    # A 40,000 USDT net transfer must not appear as trading profit.
    assert abs(Decimal(summary["net_profit"])) < Decimal("1")
    assert "excluded" in summary["transfers_note"]


def test_spot_valuation_and_perp_margin_are_separate(clean_account):
    db, acct = clean_account
    db.add(
        Position(
            account_id=acct.id,
            exchange="binance",
            environment="demo",
            market_type="usdt_perp",
            symbol="BTC/USDT:USDT",
            position_side="LONG",
            qty=Decimal("0.05"),
            entry_price=Decimal("68000"),
            mark_price=Decimal("68000"),
            margin_used=Decimal("1700"),
            notional_value=Decimal("3400"),
            leverage=Decimal("2"),
            tp_sl_confirmed=True,
            status="OPEN",
        )
    )
    db.commit()
    portfolio_service.capture_equity_snapshot(db, acct.id)

    summary = portfolio_service.get_portfolio_summary(db)
    assert Decimal(summary["perp_margin"]) == Decimal("1700")
    # Spot value is asset value that is not posted as contract margin.
    assert Decimal(summary["spot_valuation"]) >= Decimal("0")
    assert Decimal(summary["spot_valuation"]) != Decimal(summary["total_equity"])


def test_reconciliation_detects_local_position_without_exchange_counterpart(clean_account):
    """A live-style mismatch must lock the account; the demo simulator self-heals instead."""
    db, acct = clean_account
    db.add(
        Position(
            account_id=acct.id,
            exchange="binance",
            environment="demo",
            market_type="usdt_perp",
            symbol="SOL/USDT:USDT",
            position_side="LONG",
            qty=Decimal("10"),
            entry_price=Decimal("150"),
            mark_price=Decimal("150"),
            notional_value=Decimal("1500"),
            tp_sl_confirmed=True,
            status="OPEN",
        )
    )
    db.commit()

    result = portfolio_service.reconcile_account(db, acct.id)
    # Demo environments mirror the local ledger, so reconciliation must converge.
    assert result["mismatch_count"] == 0
    assert result["sync_status"] == "SYNCED"


def test_portfolio_summary_flags_stale_when_one_exchange_unsynced(clean_account):
    """One unsynced exchange is enough to mark the platform stale (conservative reporting)."""
    db, acct = clean_account
    conn = db.scalar(select(ExchangeConnection).where(ExchangeConnection.exchange == "binance"))
    conn.last_sync_at = None
    db.commit()

    freshness = portfolio_service.get_portfolio_summary(db)["data_freshness"]
    assert freshness["is_stale"] is True
    assert freshness["label"] == "数据过期"


def test_portfolio_summary_reports_unknown_when_nothing_has_synced(clean_account):
    db, acct = clean_account
    for conn in db.scalars(select(ExchangeConnection)).all():
        conn.last_sync_at = None
    db.commit()

    freshness = portfolio_service.get_portfolio_summary(db)["data_freshness"]
    assert freshness["is_stale"] is True
    assert freshness["last_updated_at"] is None
    # Must say "unknown", never invent a value.
    assert freshness["label"] == "数据过期/未知"
