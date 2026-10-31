"""
Analytics, Journal and Backtest (V1.0 §3.10, §3.11).

Backtest honesty requirements enforced in code:
- Only closed candles (`is_closed=True`) are ever used -> no lookahead bias.
- Fees, slippage and perpetual funding are always charged.
- In-sample vs out-of-sample windows are reported separately.
- Results explicitly state that they do not guarantee future returns.
"""
from __future__ import annotations

import math
import random
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.events import event_bus
from app.core.logging import get_logger
from app.core.precision import ZERO, to_decimal, utc_iso, utc_now
from app.models.entities import (
    BacktestRun,
    EquitySnapshot,
    Fill,
    JournalEntry,
    MarketCandle,
    Order,
    Position,
    Strategy,
    StrategyVersion,
)
from app.schemas.domain import BacktestCreateRequest

logger = get_logger("terminal.journal")

Q4 = Decimal("0.0001")
BPS = Decimal("10000")


def _q(value: Decimal) -> Decimal:
    return value.quantize(Q4, rounding=ROUND_HALF_UP)


def _sharpe(returns: list[float], periods_per_year: int) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(periods_per_year)


PERIODS_PER_YEAR = {"1m": 525600, "5m": 105120, "15m": 35040, "1h": 8760, "4h": 2190, "1d": 365}


class JournalAndBacktestService:
    # ------------------------------------------------------------- analytics
    def get_analytics_summary(self, db: Session) -> dict[str, Any]:
        fills = db.execute(select(Fill).order_by(Fill.occurred_at.asc())).scalars().all()
        snapshots = db.execute(select(EquitySnapshot).order_by(EquitySnapshot.observed_at.asc())).scalars().all()
        closed_positions = db.execute(select(Position).where(Position.status == "CLOSED")).scalars().all()
        orders = db.execute(select(Order)).scalars().all()
        journals = db.execute(select(JournalEntry).order_by(JournalEntry.created_at.desc())).scalars().all()

        realized = sum((to_decimal(p.realized_pnl, ZERO) for p in closed_positions), ZERO)
        fees = sum((to_decimal(f.fee, ZERO) for f in fills), ZERO)
        funding = sum((to_decimal(j.funding_pnl, ZERO) for j in journals), ZERO)
        open_positions = db.execute(select(Position).where(Position.status == "OPEN")).scalars().all()
        unrealized = sum((to_decimal(p.unrealized_pnl, ZERO) for p in open_positions), ZERO)

        wins = [to_decimal(p.realized_pnl, ZERO) for p in closed_positions if to_decimal(p.realized_pnl, ZERO) > ZERO]
        losses = [to_decimal(p.realized_pnl, ZERO) for p in closed_positions if to_decimal(p.realized_pnl, ZERO) < ZERO]
        total_trades = len(closed_positions)
        win_rate = (Decimal(len(wins)) / Decimal(total_trades) * Decimal("100")) if total_trades else ZERO
        gross_profit = sum(wins, ZERO)
        gross_loss = abs(sum(losses, ZERO))
        profit_factor = (gross_profit / gross_loss) if gross_loss > ZERO else (Decimal("999") if gross_profit > ZERO else ZERO)
        avg_win = (gross_profit / Decimal(len(wins))) if wins else ZERO
        avg_loss = (sum(losses, ZERO) / Decimal(len(losses))) if losses else ZERO

        # --- equity curve & drawdown
        curve = [
            {"observed_at": utc_iso(s.observed_at), "equity": str(s.equity), "account_id": s.account_id}
            for s in snapshots
        ]
        equities = [float(s.equity) for s in snapshots]
        max_dd_pct = 0.0
        peak = 0.0
        for eq in equities:
            peak = max(peak, eq)
            if peak > 0:
                max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100)

        returns: list[float] = []
        for prev, cur in zip(equities, equities[1:]):
            if prev:
                returns.append((cur - prev) / prev)
        sharpe = _sharpe(returns, PERIODS_PER_YEAR["15m"])

        # --- per-strategy and per-exchange
        strategy_perf: dict[str, dict[str, Any]] = {}
        for j in journals:
            bucket = strategy_perf.setdefault(
                j.strategy_version or "unknown",
                {"trades": 0, "net_pnl": ZERO, "wins": 0, "fees": ZERO},
            )
            bucket["trades"] += 1
            pnl = to_decimal(j.net_realized_pnl, ZERO)
            bucket["net_pnl"] += pnl
            bucket["fees"] += to_decimal(j.fees_paid, ZERO)
            if pnl > ZERO:
                bucket["wins"] += 1

        exchange_perf: dict[str, dict[str, Any]] = {}
        for f in fills:
            bucket = exchange_perf.setdefault(f.exchange, {"fills": 0, "volume": ZERO, "fees": ZERO, "realized_pnl": ZERO})
            bucket["fills"] += 1
            bucket["volume"] += to_decimal(f.price, ZERO) * to_decimal(f.qty, ZERO)
            bucket["fees"] += to_decimal(f.fee, ZERO)
            bucket["realized_pnl"] += to_decimal(f.realized_pnl, ZERO)

        return {
            "total_equity": str(snapshots[-1].equity if snapshots else ZERO),
            "realized_pnl": str(_q(realized)),
            "unrealized_pnl": str(_q(unrealized)),
            "fees_paid": str(_q(fees)),
            "funding_pnl": str(_q(funding)),
            "net_profit": str(_q(realized + unrealized - fees + funding)),
            "total_trades": total_trades,
            "win_rate": str(_q(win_rate)),
            "profit_factor": str(_q(profit_factor)),
            "max_drawdown_pct": str(_q(Decimal(str(round(max_dd_pct, 4))))),
            "sharpe_ratio": str(_q(Decimal(str(round(sharpe, 4))))),
            "sharpe_note": "Computed from consecutive equity snapshots sampled per 15m interval; annualized with sqrt(35040). Assumes snapshot cadence equals strategy timeframe and that returns are i.i.d.",
            "avg_win": str(_q(avg_win)),
            "avg_loss": str(_q(avg_loss)),
            "total_orders": len(orders),
            "strategy_performance": {
                k: {
                    "trades": v["trades"],
                    "net_pnl": str(_q(v["net_pnl"])),
                    "fees": str(_q(v["fees"])),
                    "win_rate": str(_q(Decimal(v["wins"]) / Decimal(v["trades"]) * Decimal("100"))) if v["trades"] else "0.0000",
                }
                for k, v in strategy_perf.items()
            },
            "exchange_performance": {
                k: {
                    "fills": v["fills"],
                    "volume": str(_q(v["volume"])),
                    "fees": str(_q(v["fees"])),
                    "realized_pnl": str(_q(v["realized_pnl"])),
                }
                for k, v in exchange_perf.items()
            },
            "generated_at": utc_iso(),
        }

    def get_equity_curve(self, db: Session, limit: int = 500) -> list[dict[str, Any]]:
        rows = db.execute(
            select(EquitySnapshot).order_by(EquitySnapshot.observed_at.asc()).limit(limit)
        ).scalars().all()
        return [
            {
                "observed_at": utc_iso(r.observed_at),
                "equity": str(r.equity),
                "realized_pnl": str(r.realized_pnl),
                "unrealized_pnl": str(r.unrealized_pnl),
                "notional_exposure": str(r.notional_exposure),
                "account_id": r.account_id,
            }
            for r in rows
        ]

    def get_strategy_performance(self, db: Session) -> dict[str, Any]:
        summary = self.get_analytics_summary(db)
        return {
            "strategy_performance": summary["strategy_performance"],
            "exchange_performance": summary["exchange_performance"],
            "sharpe_note": summary["sharpe_note"],
            "generated_at": utc_iso(),
        }

    # ----------------------------------------------------------------- journal
    def get_journal_entries(self, db: Session, limit: int = 50) -> list[dict[str, Any]]:
        rows = db.execute(select(JournalEntry).order_by(JournalEntry.created_at.desc()).limit(limit)).scalars().all()
        return [
            {
                "id": j.id,
                "account_id": j.account_id,
                "order_id": j.order_id,
                "signal_id": j.signal_id,
                "decision_id": j.decision_id,
                "strategy_id": j.strategy_id,
                "strategy_version": j.strategy_version,
                "exchange": j.exchange,
                "market_type": j.market_type,
                "symbol": j.symbol,
                "direction": j.direction,
                "ai_model_version": j.ai_model_version,
                "ai_output": j.ai_output_json,
                "risk_check": j.risk_check_json,
                "order_request": j.order_request_json,
                "exchange_order_id": j.exchange_order_id,
                "fills": j.fills_json,
                "fees_paid": str(j.fees_paid),
                "funding_pnl": str(j.funding_pnl),
                "close_reason": j.close_reason,
                "net_realized_pnl": str(j.net_realized_pnl),
                "review_summary": j.review_summary,
                "created_at": utc_iso(j.created_at),
                "chain": [
                    {"step": "signal", "ref": j.signal_id},
                    {"step": "ai_decision", "ref": j.decision_id},
                    {"step": "order", "ref": j.order_id},
                    {"step": "exchange_order", "ref": j.exchange_order_id},
                ],
            }
            for j in rows
        ]

    # ---------------------------------------------------------------- backtest
    def _load_or_synthesize_series(
        self, db: Session, req: BacktestCreateRequest
    ) -> tuple[list[tuple[Any, Decimal, Decimal, Decimal, Decimal, Decimal]], str]:
        rows = db.execute(
            select(MarketCandle)
            .where(
                MarketCandle.symbol == req.symbol,
                MarketCandle.timeframe == req.timeframe,
                MarketCandle.is_closed.is_(True),
            )
            .order_by(MarketCandle.open_time.asc())
        ).scalars().all()

        series = [
            (c.open_time, to_decimal(c.open), to_decimal(c.high), to_decimal(c.low), to_decimal(c.close), to_decimal(c.volume))
            for c in rows
        ]
        if len(series) >= req.bars_count:
            return series[-req.bars_count:], "database:market_candles(is_closed=true)"

        # Deterministic synthetic fallback: clearly labelled, never presented as real data.
        rng = random.Random(hash((req.symbol, req.timeframe)) & 0xFFFFFFFF)
        price = Decimal("68000") if "BTC" in req.symbol else (Decimal("3500") if "ETH" in req.symbol else Decimal("150"))
        step = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}[req.timeframe]
        start = utc_now() - timedelta(seconds=step * req.bars_count)
        synthetic = []
        for i in range(req.bars_count):
            drift = Decimal(str(round(rng.gauss(0.0003, 0.006), 6)))
            open_p = price
            close_p = max(Decimal("1"), (open_p * (Decimal("1") + drift)).quantize(Decimal("0.01")))
            high_p = max(open_p, close_p) * (Decimal("1") + Decimal(str(round(abs(rng.gauss(0, 0.002)), 6))))
            low_p = min(open_p, close_p) * (Decimal("1") - Decimal(str(round(abs(rng.gauss(0, 0.002)), 6))))
            vol = Decimal(str(round(abs(rng.gauss(1200, 400)), 2)))
            synthetic.append((start + timedelta(seconds=step * i), open_p, _q(high_p), _q(low_p), _q(close_p), vol))
            price = close_p
        return synthetic, "synthetic_deterministic_fallback (NOT real market data)"

    def _simulate_segment(
        self,
        series: list[tuple[Any, Decimal, Decimal, Decimal, Decimal, Decimal]],
        *,
        initial_equity: Decimal,
        fee_rate: Decimal,
        slippage_bps: Decimal,
        funding_rate_8h: Decimal,
        timeframe: str,
        is_perp: bool,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        ema_fast = int(params.get("ema_fast", 21))
        ema_slow = int(params.get("ema_slow", 50))
        atr_mult = Decimal(str(params.get("atr_multiplier", 1.8)))
        rr = Decimal(str(params.get("risk_reward_ratio", 2.0)))
        vol_threshold = Decimal(str(params.get("volume_ratio_threshold", 1.15)))
        risk_pct = Decimal(str(params.get("risk_per_trade_pct", 0.0025)))

        closes = [c[4] for c in series]
        highs = [c[2] for c in series]
        lows = [c[3] for c in series]
        volumes = [c[5] for c in series]

        def ema(vals: list[Decimal], period: int) -> list[Decimal]:
            k = Decimal("2") / Decimal(period + 1)
            out = [vals[0]]
            for v in vals[1:]:
                out.append(v * k + out[-1] * (Decimal("1") - k))
            return out

        def atr_series(period: int = 14) -> list[Decimal]:
            trs = []
            for i in range(len(series)):
                hl = highs[i] - lows[i]
                trs.append(hl if i == 0 else max(hl, abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
            return [sum(trs[max(0, i - period + 1): i + 1], ZERO) / Decimal(min(i + 1, period)) for i in range(len(trs))]

        ef, es, atrs = ema(closes, ema_fast), ema(closes, ema_slow), atr_series()

        equity = initial_equity
        peak = float(initial_equity)
        max_dd = 0.0
        position: dict[str, Any] | None = None
        trades: list[dict[str, Any]] = []
        curve: list[dict[str, Any]] = []
        bars_per_8h = max(1, int(8 * 3600 / {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}[timeframe]))

        start_idx = max(ema_slow, 20)
        for i in range(start_idx, len(series)):
            ts, o, h, l, c, v = series[i]
            equity_float = float(equity)
            peak = max(peak, equity_float)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity_float) / peak * 100)

            # ---- manage open position
            if position is not None:
                if is_perp and i % bars_per_8h == 0:
                    funding_cost = position["notional"] * funding_rate_8h
                    equity -= funding_cost
                    position["funding"] += funding_cost

                hit_sl = (l <= position["stop"]) if position["side"] == "LONG" else (h >= position["stop"])
                hit_tp = (h >= position["take_profit"]) if position["side"] == "LONG" else (l <= position["take_profit"])
                exit_price = None
                reason = None
                if hit_sl:
                    exit_price, reason = position["stop"], "STOP_LOSS"
                elif hit_tp:
                    exit_price, reason = position["take_profit"], "TAKE_PROFIT"
                if exit_price is not None:
                    slip = exit_price * (Decimal("1") - slippage_bps / BPS) if position["side"] == "LONG" else exit_price * (Decimal("1") + slippage_bps / BPS)
                    gross = (slip - position["entry"]) * position["qty"] if position["side"] == "LONG" else (position["entry"] - slip) * position["qty"]
                    exit_fee = slip * position["qty"] * fee_rate
                    net = gross - exit_fee - position["funding"]
                    equity += net
                    trades.append({
                        "side": position["side"], "entry": str(position["entry"]), "exit": str(_q(slip)),
                        "qty": str(_q(position["qty"])), "gross_pnl": str(_q(gross)), "fees": str(_q(exit_fee + position["entry_fee"])),
                        "funding": str(_q(position["funding"])), "net_pnl": str(_q(net)), "reason": reason,
                        "opened_at": utc_iso(position["opened_at"]), "closed_at": utc_iso(ts),
                    })
                    position = None
                    curve.append({"at": utc_iso(ts), "equity": str(_q(equity))})
                    continue

            if position is not None:
                curve.append({"at": utc_iso(ts), "equity": str(_q(equity + (c - position["entry"]) * position["qty"] * (1 if position["side"] == "LONG" else -1)))})
                continue

            # ---- entry logic (closed bars only; evaluate on bar i, fill at bar i close)
            avg_vol = sum(volumes[max(0, i - 20): i], ZERO) / Decimal(min(i, 20) or 1)
            vol_ok = avg_vol > ZERO and (v / avg_vol) >= vol_threshold
            atr = atrs[i]
            if atr <= ZERO or not vol_ok:
                curve.append({"at": utc_iso(ts), "equity": str(_q(equity))})
                continue

            long_signal = ef[i] > es[i] and c > ef[i]
            short_signal = is_perp and ef[i] < es[i] and c < ef[i]
            if not (long_signal or short_signal):
                curve.append({"at": utc_iso(ts), "equity": str(_q(equity))})
                continue

            side = "LONG" if long_signal else "SHORT"
            entry = c * (Decimal("1") + slippage_bps / BPS) if side == "LONG" else c * (Decimal("1") - slippage_bps / BPS)
            stop = entry - atr * atr_mult if side == "LONG" else entry + atr * atr_mult
            take = entry + atr * atr_mult * rr if side == "LONG" else entry - atr * atr_mult * rr
            risk_budget = equity * risk_pct
            stop_dist = abs(entry - stop)
            if stop_dist <= ZERO:
                curve.append({"at": utc_iso(ts), "equity": str(_q(equity))})
                continue
            qty = risk_budget / stop_dist
            entry_fee = entry * qty * fee_rate
            equity -= entry_fee
            position = {
                "side": side, "entry": entry, "stop": stop, "take_profit": take, "qty": qty,
                "notional": entry * qty, "entry_fee": entry_fee, "funding": ZERO, "opened_at": ts,
            }
            curve.append({"at": utc_iso(ts), "equity": str(_q(equity))})

        return {
            "initial_equity": str(_q(initial_equity)),
            "final_equity": str(_q(equity)),
            "net_pnl": str(_q(equity - initial_equity)),
            "return_pct": str(_q((equity - initial_equity) / initial_equity * 100)),
            "max_drawdown_pct": str(_q(Decimal(str(round(max_dd, 4))))),
            "trade_count": len(trades),
            "win_count": len([t for t in trades if Decimal(t["net_pnl"]) > ZERO]),
            "win_rate": str(_q(Decimal(len([t for t in trades if Decimal(t["net_pnl"]) > ZERO])) / Decimal(len(trades)) * 100)) if trades else "0.0000",
            "total_fees": str(_q(sum((Decimal(t["fees"]) for t in trades), ZERO))),
            "total_funding": str(_q(sum((Decimal(t["funding"]) for t in trades), ZERO))),
            "fee_share_of_gross": str(
                _q(sum((Decimal(t["fees"]) for t in trades), ZERO) / sum((abs(Decimal(t["gross_pnl"])) for t in trades), Decimal("1")) * 100)
            ) if trades else "0.0000",
            "equity_curve": curve,
            "trades": trades,
        }

    def run_backtest(self, db: Session, req: BacktestCreateRequest) -> dict[str, Any]:
        series, source = self._load_or_synthesize_series(db, req)

        strategy = None
        params: dict[str, Any] = {}
        strategy_version_id = "adhoc"
        strategy_version_label = req.strategy_code
        if req.strategy_id:
            strategy = db.scalar(select(Strategy).where(Strategy.id == req.strategy_id)) or db.scalar(
                select(Strategy).where(Strategy.code == req.strategy_id)
            )
        if strategy is None:
            strategy = db.scalar(select(Strategy).where(Strategy.code == req.strategy_code))
        if strategy is not None:
            version = db.scalar(
                select(StrategyVersion).where(
                    StrategyVersion.strategy_id == strategy.id,
                    StrategyVersion.version == strategy.current_version,
                )
            )
            if version is not None:
                strategy_version_id = version.id
                params = dict(version.parameters or {})
                strategy_version_label = f"{strategy.code}:v{version.version}"

        total = len(series)
        oos_count = max(1, int(total * float(req.out_of_sample_ratio)))
        split = total - oos_count
        in_sample_series = series[:split]
        out_sample_series = series[split:]

        common = dict(
            initial_equity=req.initial_equity,
            fee_rate=req.fee_rate,
            slippage_bps=req.slippage_bps,
            funding_rate_8h=req.funding_rate_8h,
            timeframe=req.timeframe,
            is_perp=req.market_type == "usdt_perp",
            params=params,
        )
        full = self._simulate_segment(series, **common)
        in_sample = self._simulate_segment(in_sample_series, **common)
        out_sample = self._simulate_segment(out_sample_series, **common)

        metrics = {
            "full": {k: v for k, v in full.items() if k not in ("equity_curve", "trades")},
            "in_sample": {k: v for k, v in in_sample.items() if k not in ("equity_curve", "trades")},
            "out_of_sample": {k: v for k, v in out_sample.items() if k not in ("equity_curve", "trades")},
        }

        run = BacktestRun(
            strategy_version_id=strategy_version_id,
            dataset={
                "symbol": req.symbol,
                "exchange": req.exchange,
                "market_type": req.market_type,
                "timeframe": req.timeframe,
                "bars": total,
                "in_sample_bars": len(in_sample_series),
                "out_of_sample_bars": len(out_sample_series),
                "source": source,
                "lookahead_bias": "none — only is_closed=True candles are used",
                "missing_ranges_note": "Synthetic fallback gaps are continuous by construction; DB dataset gaps are reported by candle count only.",
            },
            parameters={
                "fee_rate": str(req.fee_rate),
                "slippage_bps": str(req.slippage_bps),
                "funding_rate_8h": str(req.funding_rate_8h),
                "out_of_sample_ratio": str(req.out_of_sample_ratio),
                "strategy_version": strategy_version_label,
                **{k: str(v) for k, v in params.items()},
            },
            metrics=metrics,
            equity_curve_json=full["equity_curve"],
            trades_json=full["trades"],
            status="COMPLETED",
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        event_bus.publish_sync("system", "backtest.completed", {"id": run.id, "metrics": metrics["full"]}, source="backtest")
        return self._serialize_backtest(run)

    def list_backtests(self, db: Session, limit: int = 50) -> list[dict[str, Any]]:
        rows = db.execute(select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(limit)).scalars().all()
        return [
            {
                "id": r.id,
                "strategy_version_id": r.strategy_version_id,
                "status": r.status,
                "created_at": utc_iso(r.created_at),
                "metrics": r.metrics,
                "dataset": r.dataset,
            }
            for r in rows
        ]

    def get_backtest(self, db: Session, backtest_id: str) -> dict[str, Any] | None:
        run = db.get(BacktestRun, backtest_id)
        return self._serialize_backtest(run) if run else None

    def _serialize_backtest(self, run: BacktestRun) -> dict[str, Any]:
        return {
            "id": run.id,
            "strategy_version_id": run.strategy_version_id,
            "status": run.status,
            "dataset": run.dataset,
            "parameters": run.parameters,
            "metrics": run.metrics,
            "equity_curve": run.equity_curve_json,
            "trades": run.trades_json,
            "created_at": utc_iso(run.created_at),
            "disclaimer": "Backtest results are historical simulations only. They are not a promise or guarantee of future returns.",
        }


journal_service = JournalAndBacktestService()
