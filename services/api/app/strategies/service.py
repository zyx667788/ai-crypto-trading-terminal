from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.events import event_bus
from app.core.logging import get_logger, log_event
from app.core.precision import ZERO, round_price_to_tick, to_decimal, utc_iso, utc_now
from app.models.entities import (
    AuditLog,
    Instrument,
    MarketCandle,
    MarketSnapshot,
    Strategy,
    StrategyRun,
    StrategyVersion,
    SystemEvent,
    TradeSignal,
)
from app.scanner.service import compute_indicators_from_candles_and_snapshot
from app.schemas.domain import StrategyCreateRequest, StrategyUpdateRequest

logger = get_logger("terminal.strategies")


def compute_strategy_code_hash(
    code: str,
    parameters: dict[str, Any],
    entry_conditions: dict[str, Any],
    exit_conditions: dict[str, Any],
    risk_parameters: dict[str, Any],
) -> str:
    payload = {
        "code": code,
        "parameters": parameters or {},
        "entry_conditions": entry_conditions or {},
        "exit_conditions": exit_conditions or {},
        "risk_parameters": risk_parameters or {},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class StrategyEvaluationResult(list):
    """
    Hybrid list/dict result container for strategy evaluation:
    - Behaves as `list[TradeSignal]` for iteration, indexing (`res[0]`), and `len(res)`.
    - Also supports attribute access (`res.signals`, `res.strategy_run`, `res.signals_generated`)
      and string-key lookup (`res["signals"]`, `res.get("strategy_run")`).
    """

    def __init__(
        self,
        signals: list[TradeSignal],
        strategy_run: StrategyRun | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(signals)
        self.signals = signals
        self.strategy_run = strategy_run
        self.run = strategy_run
        self.strategy_run_id = strategy_run.id if strategy_run else None
        self.signals_generated = len(signals)
        self.summary = summary or {}
        self.deduplicated_count = int(self.summary.get("deduplicated_count", 0))
        self.cooldown_skipped_count = int(self.summary.get("cooldown_skipped_count", 0))

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, (int, slice)):
            return super().__getitem__(key)
        mapping = self.to_dict()
        if key in mapping:
            return mapping[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        mapping = self.to_dict()
        return mapping.get(key, default)

    def __contains__(self, item: Any) -> bool:
        if isinstance(item, str):
            return item in self.to_dict()
        return super().__contains__(item)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_run_id": self.strategy_run_id,
            "strategy_run": self.strategy_run,
            "signals": self.signals,
            "signals_generated": self.signals_generated,
            "deduplicated_count": self.deduplicated_count,
            "cooldown_skipped_count": self.cooldown_skipped_count,
            "summary": self.summary,
            "status": self.strategy_run.status if self.strategy_run else "COMPLETED",
        }


DEFAULT_BASELINE_STRATEGIES: tuple[dict[str, Any], ...] = (
    {
        "code": "trend_following_v1",
        "name": "EMA 趋势跟随 + ATR 止损策略",
        "description": (
            "基于已收盘 K 线的可解释趋势跟随规则策略：EMA(21) 与 EMA(50) 方向对齐、"
            "20 周期成交量放大确认、RSI 处于健康趋势区间，使用 ATR 动态计算止损与盈亏比止盈。"
        ),
        "exchange": "binance",
        "market_type": "usdt_perp",
        "symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
        "timeframe": "15m",
        "cooldown_seconds": 900,
        "max_positions": 2,
        "parameters": {
            "ema_fast": 21,
            "ema_slow": 50,
            "rsi_period": 14,
            "rsi_long_min": "40.0",
            "rsi_long_max": "82.0",
            "rsi_short_min": "18.0",
            "rsi_short_max": "60.0",
            "volume_ratio_threshold": "0.85",
            "atr_period": 14,
            "atr_multiplier": "2.0",
            "rr_ratio": "2.0",
            "only_closed_candles": True,
        },
        "entry_conditions": {
            "long": "EMA(21) > EMA(50) AND volume_ratio_20 >= threshold AND RSI(14) in [40, 82]",
            "short": "EMA(21) < EMA(50) AND volume_ratio_20 >= threshold AND RSI(14) in [18, 60] (usdt_perp only)",
        },
        "exit_conditions": {
            "stop_loss": "entry_price -/+ atr_multiplier * ATR(14)",
            "take_profit": "entry_price +/- rr_ratio * atr_multiplier * ATR(14)",
            "invalidation": "EMA(21) crosses against trend or closed candle breaks ATR stop",
        },
        "risk_parameters": {
            "max_risk_per_trade_pct": "0.0025",
            "max_leverage": "2",
            "require_closed_candle": True,
        },
    },
    {
        "code": "breakout_v1",
        "name": "区间高低点突破 + 成交量确认策略",
        "description": (
            "基于已收盘 K 线的区间突破策略：收盘价突破前 N 根已收盘 K 线高低点、"
            "成交量比率放大确认、ATR 波动率过滤，并按 ATR 倍数设置止损与止盈。"
        ),
        "exchange": "binance",
        "market_type": "usdt_perp",
        "symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
        "timeframe": "15m",
        "cooldown_seconds": 900,
        "max_positions": 2,
        "parameters": {
            "lookback_bars": 20,
            "volume_ratio_threshold": "0.85",
            "min_volatility": "0.0005",
            "atr_period": 14,
            "atr_multiplier": "1.8",
            "rr_ratio": "2.2",
            "only_closed_candles": True,
        },
        "entry_conditions": {
            "long": "Closed candle close > N-bar high (or range close high) AND volume_ratio_20 >= threshold AND volatility >= min_volatility",
            "short": "Closed candle close < N-bar low (or range close low) AND volume_ratio_20 >= threshold AND volatility >= min_volatility (usdt_perp only)",
        },
        "exit_conditions": {
            "stop_loss": "entry_price -/+ atr_multiplier * ATR(14)",
            "take_profit": "entry_price +/- rr_ratio * atr_multiplier * ATR(14)",
            "invalidation": "Close falls back inside breakout range or hits ATR stop",
        },
        "risk_parameters": {
            "max_risk_per_trade_pct": "0.0025",
            "max_leverage": "2",
            "require_closed_candle": True,
        },
    },
)


class StrategyService:
    """
    Strategy Engine Service (Section 3.5):
    - Seeds baseline explainable strategies (`trend_following_v1`, `breakout_v1`)
    - Enforces immutable strategy versioning (`StrategyVersion` with SHA-256 `code_hash`) and `AuditLog`
    - Evaluates strategies strictly on closed candles (`is_closed == True`)
    - Enforces deterministic signal deduplication (`dedup_key`) and strategy `cooldown_seconds`
    """

    def ensure_default_strategies(self, db: Session) -> list[Strategy]:
        created_or_found: list[Strategy] = []
        changed = False

        for spec in DEFAULT_BASELINE_STRATEGIES:
            stmt = select(Strategy).where(Strategy.code == spec["code"])
            strategy = db.scalar(stmt)
            if strategy is None:
                code_hash = compute_strategy_code_hash(
                    code=spec["code"],
                    parameters=spec["parameters"],
                    entry_conditions=spec["entry_conditions"],
                    exit_conditions=spec["exit_conditions"],
                    risk_parameters=spec["risk_parameters"],
                )
                strategy = Strategy(
                    owner_id="local_admin",
                    code=spec["code"],
                    name=spec["name"],
                    description=spec["description"],
                    exchange=spec["exchange"],
                    market_type=spec["market_type"],
                    symbols_json=list(spec["symbols"]),
                    timeframe=spec["timeframe"],
                    status="ACTIVE",
                    current_version=1,
                    cooldown_seconds=spec["cooldown_seconds"],
                    max_positions=spec["max_positions"],
                    config={
                        "parameters": spec["parameters"],
                        "entry_conditions": spec["entry_conditions"],
                        "exit_conditions": spec["exit_conditions"],
                        "risk_parameters": spec["risk_parameters"],
                        "code_hash": code_hash,
                    },
                )
                db.add(strategy)
                db.flush()

                v1 = StrategyVersion(
                    strategy_id=strategy.id,
                    version=1,
                    parameters=dict(spec["parameters"]),
                    entry_conditions=dict(spec["entry_conditions"]),
                    exit_conditions=dict(spec["exit_conditions"]),
                    risk_parameters=dict(spec["risk_parameters"]),
                    code_hash=code_hash,
                    change_note="Initial baseline strategy v1",
                )
                db.add(v1)
                changed = True
            else:
                ver_stmt = select(StrategyVersion).where(
                    StrategyVersion.strategy_id == strategy.id,
                    StrategyVersion.version == strategy.current_version,
                )
                existing_ver = db.scalar(ver_stmt)
                if existing_ver is None:
                    code_hash = compute_strategy_code_hash(
                        code=strategy.code,
                        parameters=spec["parameters"],
                        entry_conditions=spec["entry_conditions"],
                        exit_conditions=spec["exit_conditions"],
                        risk_parameters=spec["risk_parameters"],
                    )
                    v_row = StrategyVersion(
                        strategy_id=strategy.id,
                        version=strategy.current_version,
                        parameters=dict(spec["parameters"]),
                        entry_conditions=dict(spec["entry_conditions"]),
                        exit_conditions=dict(spec["exit_conditions"]),
                        risk_parameters=dict(spec["risk_parameters"]),
                        code_hash=code_hash,
                        change_note="Seeded version record",
                    )
                    db.add(v_row)
                    changed = True

            created_or_found.append(strategy)

        if changed:
            db.commit()
            for s in created_or_found:
                db.refresh(s)

        return created_or_found

    def _get_strategy_or_raise(self, db: Session, strategy_id_or_code: str) -> Strategy:
        stmt = select(Strategy).where(
            (Strategy.id == strategy_id_or_code) | (Strategy.code == strategy_id_or_code)
        )
        strategy = db.scalar(stmt)
        if strategy is None:
            self.ensure_default_strategies(db)
            strategy = db.scalar(stmt)
        if strategy is None:
            raise ValueError(f"Strategy not found: {strategy_id_or_code}")
        return strategy

    def get_current_version_row(self, db: Session, strategy: Strategy) -> StrategyVersion:
        stmt = select(StrategyVersion).where(
            StrategyVersion.strategy_id == strategy.id,
            StrategyVersion.version == strategy.current_version,
        )
        ver = db.scalar(stmt)
        if ver is None:
            cfg = strategy.config or {}
            params = cfg.get("parameters", {})
            entry_cond = cfg.get("entry_conditions", {})
            exit_cond = cfg.get("exit_conditions", {})
            risk_params = cfg.get("risk_parameters", {})
            code_hash = compute_strategy_code_hash(
                strategy.code, params, entry_cond, exit_cond, risk_params
            )
            ver = StrategyVersion(
                strategy_id=strategy.id,
                version=strategy.current_version,
                parameters=params,
                entry_conditions=entry_cond,
                exit_conditions=exit_cond,
                risk_parameters=risk_params,
                code_hash=code_hash,
                change_note="Recovered current version",
            )
            db.add(ver)
            db.commit()
            db.refresh(ver)
        return ver

    def list_strategies(self, db: Session) -> list[dict[str, Any]]:
        self.ensure_default_strategies(db)
        strategies = list(db.scalars(select(Strategy).order_by(Strategy.created_at.asc())).all())
        result: list[dict[str, Any]] = []
        for s in strategies:
            ver = self.get_current_version_row(db, s)
            result.append(self._serialize_strategy(db, s, ver, include_history=True))
        return result

    def get_strategy(self, db: Session, strategy_id: str) -> dict[str, Any]:
        strategy = self._get_strategy_or_raise(db, strategy_id)
        ver = self.get_current_version_row(db, strategy)
        return self._serialize_strategy(db, strategy, ver, include_history=True)

    def create_strategy(
        self,
        db: Session,
        req: StrategyCreateRequest | dict[str, Any],
    ) -> Strategy:
        payload = req if isinstance(req, StrategyCreateRequest) else StrategyCreateRequest.model_validate(req)
        existing = db.scalar(select(Strategy).where(Strategy.code == payload.code))
        if existing is not None:
            raise ValueError(f"Strategy with code '{payload.code}' already exists")

        code_hash = compute_strategy_code_hash(
            code=payload.code,
            parameters=payload.parameters,
            entry_conditions=payload.entry_conditions,
            exit_conditions=payload.exit_conditions,
            risk_parameters=payload.risk_parameters,
        )
        strategy = Strategy(
            owner_id="local_admin",
            code=payload.code,
            name=payload.name,
            description=payload.description,
            exchange=payload.exchange,
            market_type=payload.market_type,
            symbols_json=list(payload.symbols),
            timeframe=payload.timeframe,
            status="ACTIVE",
            current_version=1,
            cooldown_seconds=payload.cooldown_seconds,
            max_positions=payload.max_positions,
            config={
                "parameters": payload.parameters,
                "entry_conditions": payload.entry_conditions,
                "exit_conditions": payload.exit_conditions,
                "risk_parameters": payload.risk_parameters,
                "code_hash": code_hash,
            },
        )
        db.add(strategy)
        db.flush()

        ver = StrategyVersion(
            strategy_id=strategy.id,
            version=1,
            parameters=dict(payload.parameters),
            entry_conditions=dict(payload.entry_conditions),
            exit_conditions=dict(payload.exit_conditions),
            risk_parameters=dict(payload.risk_parameters),
            code_hash=code_hash,
            change_note="Initial version v1",
        )
        db.add(ver)

        db.add(
            AuditLog(
                actor="local_admin",
                action="CREATE_STRATEGY",
                resource=f"strategy:{strategy.id}",
                before_json={},
                after_json={
                    "id": strategy.id,
                    "code": strategy.code,
                    "version": 1,
                    "code_hash": code_hash,
                },
            )
        )
        db.commit()
        db.refresh(strategy)
        return strategy

    def update_strategy(
        self,
        db: Session,
        strategy_id: str,
        req: StrategyUpdateRequest | dict[str, Any],
    ) -> Strategy:
        """
        Immutable Strategy Versioning (Section 3.5):
        Increments `strategy.current_version` and inserts a new `StrategyVersion` row
        with a SHA-256 `code_hash` without overwriting previous `StrategyVersion` rows.
        Records an `AuditLog`.
        """
        payload = req if isinstance(req, StrategyUpdateRequest) else StrategyUpdateRequest.model_validate(req)
        strategy = self._get_strategy_or_raise(db, strategy_id)
        prev_ver = self.get_current_version_row(db, strategy)

        before_snapshot = {
            "strategy_id": strategy.id,
            "code": strategy.code,
            "name": strategy.name,
            "description": strategy.description,
            "symbols": list(strategy.symbols_json or []),
            "timeframe": strategy.timeframe,
            "cooldown_seconds": strategy.cooldown_seconds,
            "max_positions": strategy.max_positions,
            "version": prev_ver.version,
            "parameters": dict(prev_ver.parameters or {}),
            "entry_conditions": dict(prev_ver.entry_conditions or {}),
            "exit_conditions": dict(prev_ver.exit_conditions or {}),
            "risk_parameters": dict(prev_ver.risk_parameters or {}),
            "code_hash": prev_ver.code_hash,
        }

        new_parameters = dict(prev_ver.parameters or {})
        if payload.parameters:
            new_parameters.update(payload.parameters)

        new_entry_conditions = dict(prev_ver.entry_conditions or {})
        if payload.entry_conditions:
            new_entry_conditions.update(payload.entry_conditions)

        new_exit_conditions = dict(prev_ver.exit_conditions or {})
        if payload.exit_conditions:
            new_exit_conditions.update(payload.exit_conditions)

        new_risk_parameters = dict(prev_ver.risk_parameters or {})
        if payload.risk_parameters:
            new_risk_parameters.update(payload.risk_parameters)

        if payload.name is not None:
            strategy.name = payload.name
        if payload.description is not None:
            strategy.description = payload.description
        if payload.symbols is not None:
            strategy.symbols_json = list(payload.symbols)
        if payload.timeframe is not None:
            strategy.timeframe = payload.timeframe
        if payload.cooldown_seconds is not None:
            strategy.cooldown_seconds = int(payload.cooldown_seconds)
        if payload.max_positions is not None:
            strategy.max_positions = int(payload.max_positions)

        next_version_number = int(strategy.current_version) + 1
        strategy.current_version = next_version_number
        strategy.updated_at = utc_now()

        new_code_hash = compute_strategy_code_hash(
            code=strategy.code,
            parameters=new_parameters,
            entry_conditions=new_entry_conditions,
            exit_conditions=new_exit_conditions,
            risk_parameters=new_risk_parameters,
        )

        strategy.config = {
            "parameters": new_parameters,
            "entry_conditions": new_entry_conditions,
            "exit_conditions": new_exit_conditions,
            "risk_parameters": new_risk_parameters,
            "code_hash": new_code_hash,
        }

        new_version_row = StrategyVersion(
            strategy_id=strategy.id,
            version=next_version_number,
            parameters=new_parameters,
            entry_conditions=new_entry_conditions,
            exit_conditions=new_exit_conditions,
            risk_parameters=new_risk_parameters,
            code_hash=new_code_hash,
            change_note=payload.change_note or f"Updated to v{next_version_number}",
        )
        db.add(new_version_row)
        db.flush()

        after_snapshot = {
            "strategy_id": strategy.id,
            "code": strategy.code,
            "name": strategy.name,
            "description": strategy.description,
            "symbols": list(strategy.symbols_json or []),
            "timeframe": strategy.timeframe,
            "cooldown_seconds": strategy.cooldown_seconds,
            "max_positions": strategy.max_positions,
            "version": next_version_number,
            "version_id": new_version_row.id,
            "parameters": new_parameters,
            "entry_conditions": new_entry_conditions,
            "exit_conditions": new_exit_conditions,
            "risk_parameters": new_risk_parameters,
            "code_hash": new_code_hash,
            "change_note": new_version_row.change_note,
        }

        db.add(
            AuditLog(
                actor="local_admin",
                action="UPDATE_STRATEGY",
                resource=f"strategy:{strategy.id}",
                before_json=before_snapshot,
                after_json=after_snapshot,
            )
        )
        db.commit()
        db.refresh(strategy)
        log_event(
            logger,
            20,
            "Strategy updated with immutable version",
            strategy_id=strategy.id,
            code=strategy.code,
            new_version=next_version_number,
            code_hash=new_code_hash,
        )
        return strategy

    def start_strategy(self, db: Session, strategy_id: str, evaluate_now: bool = True) -> dict[str, Any]:
        strategy = self._get_strategy_or_raise(db, strategy_id)
        strategy.status = "ACTIVE"
        strategy.updated_at = utc_now()
        db.add(
            AuditLog(
                actor="local_admin",
                action="START_STRATEGY",
                resource=f"strategy:{strategy.id}",
                before_json={"status": "PAUSED"},
                after_json={"status": "ACTIVE", "current_version": strategy.current_version},
            )
        )
        db.commit()
        eval_res: StrategyEvaluationResult | None = None
        if evaluate_now:
            eval_res = self.evaluate_strategy(db, strategy.id)
        ver = self.get_current_version_row(db, strategy)
        out = self._serialize_strategy(db, strategy, ver, include_history=True)
        out["signals_generated"] = len(eval_res) if eval_res is not None else 0
        out["signals"] = [self._serialize_signal(s) for s in (eval_res or [])]
        return out

    def pause_strategy(self, db: Session, strategy_id: str) -> dict[str, Any]:
        strategy = self._get_strategy_or_raise(db, strategy_id)
        prev_status = strategy.status
        strategy.status = "PAUSED"
        strategy.updated_at = utc_now()
        db.add(
            AuditLog(
                actor="local_admin",
                action="PAUSE_STRATEGY",
                resource=f"strategy:{strategy.id}",
                before_json={"status": prev_status},
                after_json={"status": "PAUSED"},
            )
        )
        db.commit()
        ver = self.get_current_version_row(db, strategy)
        return self._serialize_strategy(db, strategy, ver, include_history=True)

    def _evaluate_symbol_rules(
        self,
        *,
        strategy: Strategy,
        version_row: StrategyVersion,
        symbol: str,
        closed_candles: list[MarketCandle],
        snapshot: MarketSnapshot | None,
        instrument: Instrument | None,
    ) -> dict[str, Any] | None:
        """
        Evaluate either `trend_following_v1` or `breakout_v1` strictly on closed candles (`is_closed=True`).
        Returns candidate signal dict if triggered, or None.
        """
        if not closed_candles:
            return None

        indicators = compute_indicators_from_candles_and_snapshot(closed_candles, snapshot)
        latest_candle = closed_candles[-1]
        entry_price = to_decimal(latest_candle.close, ZERO)
        if entry_price <= ZERO:
            return None

        tick_size = to_decimal(instrument.tick_size, Decimal("0.01")) if instrument else Decimal("0.01")
        params = dict(version_row.parameters or {})

        ema_21 = to_decimal(indicators["ema_21"], entry_price)
        ema_50 = to_decimal(indicators["ema_50"], entry_price)
        rsi_14 = to_decimal(indicators["rsi_14"], Decimal("55"))
        macd_hist = to_decimal(indicators["macd_hist"], ZERO)
        atr_14 = to_decimal(indicators["atr_14"], max(entry_price * Decimal("0.008"), tick_size * Decimal("10")))
        if atr_14 <= ZERO:
            atr_14 = max(entry_price * Decimal("0.008"), tick_size * Decimal("10"))
        volume_ratio_20 = to_decimal(indicators["volume_ratio_20"], Decimal("1.1"))
        volatility = to_decimal(indicators["volatility"], Decimal("0.005"))

        atr_multiplier = to_decimal(params.get("atr_multiplier", "2.0"), Decimal("2.0"))
        rr_ratio = to_decimal(params.get("rr_ratio", "2.0"), Decimal("2.0"))
        vol_threshold = to_decimal(params.get("volume_ratio_threshold", "0.85"), Decimal("0.85"))

        direction: str | None = None
        trigger_reason = ""
        score = Decimal("75.00")

        is_breakout = strategy.code == "breakout_v1" or "breakout" in strategy.code.lower()

        if not is_breakout:
            # Strategy 1: trend_following_v1 ("EMA 趋势跟随 + ATR 止损策略")
            rsi_long_min = to_decimal(params.get("rsi_long_min", "40.0"), Decimal("40.0"))
            rsi_long_max = to_decimal(params.get("rsi_long_max", "85.0"), Decimal("85.0"))
            rsi_short_min = to_decimal(params.get("rsi_short_min", "15.0"), Decimal("15.0"))
            rsi_short_max = to_decimal(params.get("rsi_short_max", "60.0"), Decimal("60.0"))

            rsi_long_ok = (rsi_long_min <= rsi_14 <= rsi_long_max) or (ema_21 > ema_50 and macd_hist >= ZERO)
            rsi_short_ok = (rsi_short_min <= rsi_14 <= rsi_short_max) or (ema_21 < ema_50 and macd_hist <= ZERO)

            if ema_21 > ema_50 and volume_ratio_20 >= vol_threshold and rsi_long_ok:
                direction = "LONG" if strategy.market_type == "usdt_perp" else "BUY"
                score = min(
                    Decimal("95.00"),
                    Decimal("72.00")
                    + (Decimal("8.00") if macd_hist > ZERO else ZERO)
                    + (Decimal("8.00") if volume_ratio_20 >= Decimal("1.15") else Decimal("3.00")),
                )
                trigger_reason = (
                    f"[trend_following_v1] EMA(21)={ema_21} > EMA(50)={ema_50}, "
                    f"RSI(14)={rsi_14} in healthy trend zone, volume_ratio={volume_ratio_20}x >= {vol_threshold}x"
                )
            elif (
                strategy.market_type == "usdt_perp"
                and ema_21 < ema_50
                and volume_ratio_20 >= vol_threshold
                and rsi_short_ok
            ):
                direction = "SHORT"
                score = min(
                    Decimal("95.00"),
                    Decimal("72.00")
                    + (Decimal("8.00") if macd_hist < ZERO else ZERO)
                    + (Decimal("8.00") if volume_ratio_20 >= Decimal("1.15") else Decimal("3.00")),
                )
                trigger_reason = (
                    f"[trend_following_v1] EMA(21)={ema_21} < EMA(50)={ema_50}, "
                    f"RSI(14)={rsi_14} in bearish trend zone, volume_ratio={volume_ratio_20}x >= {vol_threshold}x"
                )
        else:
            # Strategy 2: breakout_v1 ("区间高低点突破 + 成交量确认策略")
            lookback_bars = int(params.get("lookback_bars", 20))
            min_volatility = to_decimal(params.get("min_volatility", "0.0005"), Decimal("0.0005"))
            prior_bars = (
                closed_candles[-(lookback_bars + 1) : -1]
                if len(closed_candles) > lookback_bars
                else closed_candles[:-1]
            )
            if not prior_bars:
                prior_bars = closed_candles

            range_high = max(to_decimal(c.high, entry_price) for c in prior_bars)
            range_low = min(to_decimal(c.low, entry_price) for c in prior_bars)
            range_close_high = max(to_decimal(c.close, entry_price) for c in prior_bars)
            range_close_low = min(to_decimal(c.close, entry_price) for c in prior_bars)

            broke_high = entry_price > range_high or (
                entry_price >= range_close_high and to_decimal(latest_candle.high, entry_price) >= range_high
            )
            broke_low = entry_price < range_low or (
                entry_price <= range_close_low and to_decimal(latest_candle.low, entry_price) <= range_low
            )

            if broke_high and volume_ratio_20 >= vol_threshold and volatility >= min_volatility:
                direction = "LONG" if strategy.market_type == "usdt_perp" else "BUY"
                score = min(
                    Decimal("96.00"),
                    Decimal("75.00")
                    + (Decimal("10.00") if entry_price > range_high else Decimal("5.00"))
                    + (Decimal("6.00") if volume_ratio_20 >= Decimal("1.20") else Decimal("2.00")),
                )
                trigger_reason = (
                    f"[breakout_v1] Closed candle close {entry_price} broke {len(prior_bars)}-bar high "
                    f"(range_high={range_high}, close_high={range_close_high}), "
                    f"volume_ratio={volume_ratio_20}x, volatility={volatility}"
                )
            elif (
                strategy.market_type == "usdt_perp"
                and broke_low
                and volume_ratio_20 >= vol_threshold
                and volatility >= min_volatility
            ):
                direction = "SHORT"
                score = min(
                    Decimal("96.00"),
                    Decimal("75.00")
                    + (Decimal("10.00") if entry_price < range_low else Decimal("5.00"))
                    + (Decimal("6.00") if volume_ratio_20 >= Decimal("1.20") else Decimal("2.00")),
                )
                trigger_reason = (
                    f"[breakout_v1] Closed candle close {entry_price} broke {len(prior_bars)}-bar low "
                    f"(range_low={range_low}, close_low={range_close_low}), "
                    f"volume_ratio={volume_ratio_20}x, volatility={volatility}"
                )

        if direction is None:
            return None

        stop_offset = max(atr_multiplier * atr_14, tick_size * Decimal("5"))
        tp_offset = max(rr_ratio * stop_offset, tick_size * Decimal("10"))
        rounded_entry = round_price_to_tick(entry_price, tick_size)

        if direction in ("LONG", "BUY"):
            stop_loss = round_price_to_tick(max(rounded_entry - stop_offset, tick_size), tick_size)
            if stop_loss >= rounded_entry:
                stop_loss = max(rounded_entry - tick_size, tick_size)
            take_profit = round_price_to_tick(rounded_entry + tp_offset, tick_size)
            if take_profit <= rounded_entry:
                take_profit = rounded_entry + tick_size * Decimal("2")
        else:
            stop_loss = round_price_to_tick(rounded_entry + stop_offset, tick_size)
            if stop_loss <= rounded_entry:
                stop_loss = rounded_entry + tick_size
            take_profit = round_price_to_tick(max(rounded_entry - tp_offset, tick_size), tick_size)
            if take_profit >= rounded_entry:
                take_profit = max(rounded_entry - tick_size * Decimal("2"), tick_size)

        enriched_indicators = dict(indicators)
        enriched_indicators.update(
            {
                "entry_price": str(rounded_entry),
                "stop_loss": str(stop_loss),
                "take_profit": str(take_profit),
                "atr_multiplier": str(atr_multiplier),
                "rr_ratio": str(rr_ratio),
                "strategy_code": strategy.code,
                "strategy_version": f"{strategy.code}:v{strategy.current_version}",
                "code_hash": version_row.code_hash,
                "latest_candle_open_time": utc_iso(latest_candle.open_time),
                "latest_candle_close_time": utc_iso(latest_candle.close_time),
            }
        )

        return {
            "direction": direction,
            "trigger_reason": trigger_reason,
            "score": score.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            "indicators_snapshot": enriched_indicators,
            "latest_candle_open_time": utc_iso(latest_candle.open_time),
        }

    def evaluate_strategy(self, db: Session, strategy_id: str) -> StrategyEvaluationResult:
        """
        Evaluate a strategy strictly on closed candles (`is_closed=True`), enforcing:
        1. Signal deduplication via `dedup_key = f"{strategy.id}:{strategy.current_version}:{symbol}:{timeframe}:{direction}:{latest_candle_open_time}"`
        2. Strategy cooldown (`cooldown_seconds`)
        Creates `StrategyRun` and `TradeSignal` rows with `expires_at`.
        """
        strategy = self._get_strategy_or_raise(db, strategy_id)
        version_row = self.get_current_version_row(db, strategy)

        started_at = utc_now()
        strategy_run = StrategyRun(
            strategy_id=strategy.id,
            strategy_version_id=version_row.id,
            started_at=started_at,
            status="RUNNING",
            signals_generated=0,
            summary_json={},
        )
        db.add(strategy_run)
        db.flush()

        target_symbols = list(strategy.symbols_json or [])
        if not target_symbols:
            target_symbols = ["BTC/USDT:USDT", "ETH/USDT:USDT"]

        created_signals: list[TradeSignal] = []
        deduplicated_count = 0
        cooldown_skipped_count = 0
        evaluated_symbols: list[str] = []

        for symbol in target_symbols:
            # Strictly select ONLY closed candles (is_closed == True)
            candle_stmt = (
                select(MarketCandle)
                .where(
                    MarketCandle.exchange == strategy.exchange,
                    MarketCandle.market_type == strategy.market_type,
                    MarketCandle.symbol == symbol,
                    MarketCandle.timeframe == strategy.timeframe,
                    MarketCandle.is_closed.is_(True),
                )
                .order_by(MarketCandle.open_time.asc())
            )
            closed_candles = list(db.scalars(candle_stmt).all())
            if not closed_candles:
                # Fallback to any timeframe's closed candles for that exchange/market_type/symbol if timeframe differs
                alt_stmt = (
                    select(MarketCandle)
                    .where(
                        MarketCandle.exchange == strategy.exchange,
                        MarketCandle.market_type == strategy.market_type,
                        MarketCandle.symbol == symbol,
                        MarketCandle.is_closed.is_(True),
                    )
                    .order_by(MarketCandle.open_time.asc())
                )
                closed_candles = list(db.scalars(alt_stmt).all())

            if not closed_candles:
                continue

            evaluated_symbols.append(symbol)
            inst = db.scalar(
                select(Instrument).where(
                    Instrument.exchange == strategy.exchange,
                    Instrument.market_type == strategy.market_type,
                    Instrument.canonical_symbol == symbol,
                )
            )
            snap = db.scalar(
                select(MarketSnapshot)
                .where(
                    MarketSnapshot.exchange == strategy.exchange,
                    MarketSnapshot.market_type == strategy.market_type,
                    MarketSnapshot.symbol == symbol,
                )
                .order_by(MarketSnapshot.event_time.desc())
            )

            candidate = self._evaluate_symbol_rules(
                strategy=strategy,
                version_row=version_row,
                symbol=symbol,
                closed_candles=closed_candles,
                snapshot=snap,
                instrument=inst,
            )
            if candidate is None:
                continue

            direction = candidate["direction"]
            latest_open_time_str = candidate["latest_candle_open_time"]
            effective_tf = closed_candles[-1].timeframe or strategy.timeframe

            dedup_key = (
                f"{strategy.id}:{strategy.current_version}:{symbol}:"
                f"{effective_tf}:{direction}:{latest_open_time_str}"
            )
            if len(dedup_key) > 128:
                digest = hashlib.sha256(dedup_key.encode("utf-8")).hexdigest()[:24]
                dedup_key = f"{strategy.id[:8]}:v{strategy.current_version}:{symbol}:{direction}:{digest}"

            # 1. Deduplication check by exact dedup_key
            existing_dedup = db.scalar(select(TradeSignal).where(TradeSignal.dedup_key == dedup_key))
            if existing_dedup is not None:
                deduplicated_count += 1
                continue

            # 2. Cooldown check within strategy.cooldown_seconds across all versions of this strategy
            if strategy.cooldown_seconds > 0:
                cooldown_cutoff = utc_now() - timedelta(seconds=int(strategy.cooldown_seconds))
                version_ids_stmt = select(StrategyVersion.id).where(StrategyVersion.strategy_id == strategy.id)
                recent_signal = db.scalar(
                    select(TradeSignal)
                    .where(
                        TradeSignal.strategy_version_id.in_(version_ids_stmt),
                        TradeSignal.symbol == symbol,
                        TradeSignal.direction == direction,
                        TradeSignal.created_at >= cooldown_cutoff,
                    )
                    .order_by(TradeSignal.created_at.desc())
                )
                if recent_signal is not None:
                    cooldown_skipped_count += 1
                    continue

            ttl_seconds = max(int(strategy.cooldown_seconds), 1800)
            expires_at = utc_now() + timedelta(seconds=ttl_seconds)

            signal = TradeSignal(
                strategy_run_id=strategy_run.id,
                strategy_version_id=version_row.id,
                decision_id=None,
                instrument_id=inst.id if inst else None,
                exchange=strategy.exchange,
                market_type=strategy.market_type,
                symbol=symbol,
                timeframe=effective_tf,
                direction=direction,
                trigger_reason=candidate["trigger_reason"],
                indicators_snapshot=candidate["indicators_snapshot"],
                score=candidate["score"],
                dedup_key=dedup_key,
                status="ACTIVE",
                expires_at=expires_at,
            )
            db.add(signal)
            db.flush()
            created_signals.append(signal)

        ended_at = utc_now()
        summary = {
            "strategy_id": strategy.id,
            "strategy_code": strategy.code,
            "version": strategy.current_version,
            "code_hash": version_row.code_hash,
            "evaluated_symbols": evaluated_symbols,
            "signals_generated": len(created_signals),
            "deduplicated_count": deduplicated_count,
            "cooldown_skipped_count": cooldown_skipped_count,
            "only_closed_candles": True,
        }
        strategy_run.ended_at = ended_at
        strategy_run.status = "COMPLETED"
        strategy_run.signals_generated = len(created_signals)
        strategy_run.summary_json = summary

        if created_signals:
            db.add(
                SystemEvent(
                    component="strategies",
                    event_type="STRATEGY_SIGNALS_GENERATED",
                    severity="INFO",
                    payload={
                        "strategy_id": strategy.id,
                        "strategy_code": strategy.code,
                        "version": strategy.current_version,
                        "signals_count": len(created_signals),
                        "signal_ids": [s.id for s in created_signals],
                    },
                )
            )

        db.commit()
        for sig in created_signals:
            db.refresh(sig)
            event_bus.publish_sync(
                channel="scanner",
                event_type="strategy.signal_generated",
                data=self._serialize_signal(sig),
                source="strategies",
            )
        db.refresh(strategy_run)

        return StrategyEvaluationResult(
            signals=created_signals,
            strategy_run=strategy_run,
            summary=summary,
        )

    def evaluate_all_active_strategies(self, db: Session) -> StrategyEvaluationResult:
        self.ensure_default_strategies(db)
        active_strategies = list(
            db.scalars(select(Strategy).where(Strategy.status == "ACTIVE").order_by(Strategy.created_at.asc())).all()
        )
        all_signals: list[TradeSignal] = []
        last_run: StrategyRun | None = None
        total_dedup = 0
        total_cooldown = 0
        runs_summary: list[dict[str, Any]] = []

        for strategy in active_strategies:
            res = self.evaluate_strategy(db, strategy.id)
            all_signals.extend(res.signals)
            if res.strategy_run is not None:
                last_run = res.strategy_run
            total_dedup += res.deduplicated_count
            total_cooldown += res.cooldown_skipped_count
            runs_summary.append(res.summary)

        combined_summary = {
            "active_strategies_evaluated": len(active_strategies),
            "signals_generated": len(all_signals),
            "deduplicated_count": total_dedup,
            "cooldown_skipped_count": total_cooldown,
            "runs": runs_summary,
        }
        return StrategyEvaluationResult(
            signals=all_signals,
            strategy_run=last_run,
            summary=combined_summary,
        )

    def list_signals(
        self,
        db: Session,
        strategy_id: str | None = None,
        symbol: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        stmt = select(TradeSignal).order_by(TradeSignal.created_at.desc())
        if strategy_id:
            ver_ids = select(StrategyVersion.id).where(StrategyVersion.strategy_id == strategy_id)
            stmt = stmt.where(TradeSignal.strategy_version_id.in_(ver_ids))
        if symbol:
            stmt = stmt.where(TradeSignal.symbol == symbol)
        if status:
            stmt = stmt.where(TradeSignal.status == status)
        rows = list(db.scalars(stmt.limit(limit)).all())
        return [self._serialize_signal(r) for r in rows]

    def _serialize_signal(self, sig: TradeSignal) -> dict[str, Any]:
        return {
            "id": sig.id,
            "strategy_run_id": sig.strategy_run_id,
            "strategy_version_id": sig.strategy_version_id,
            "decision_id": sig.decision_id,
            "instrument_id": sig.instrument_id,
            "exchange": sig.exchange,
            "market_type": sig.market_type,
            "symbol": sig.symbol,
            "timeframe": sig.timeframe,
            "direction": sig.direction,
            "trigger_reason": sig.trigger_reason,
            "indicators_snapshot": sig.indicators_snapshot,
            "score": str(sig.score),
            "dedup_key": sig.dedup_key,
            "status": sig.status,
            "expires_at": utc_iso(sig.expires_at),
            "created_at": utc_iso(sig.created_at),
        }

    def _serialize_strategy(
        self,
        db: Session,
        strategy: Strategy,
        current_ver: StrategyVersion,
        include_history: bool = False,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": strategy.id,
            "owner_id": strategy.owner_id,
            "code": strategy.code,
            "name": strategy.name,
            "description": strategy.description,
            "exchange": strategy.exchange,
            "market_type": strategy.market_type,
            "symbols": list(strategy.symbols_json or []),
            "timeframe": strategy.timeframe,
            "status": strategy.status,
            "current_version": strategy.current_version,
            "cooldown_seconds": strategy.cooldown_seconds,
            "max_positions": strategy.max_positions,
            "code_hash": current_ver.code_hash,
            "parameters": current_ver.parameters,
            "entry_conditions": current_ver.entry_conditions,
            "exit_conditions": current_ver.exit_conditions,
            "risk_parameters": current_ver.risk_parameters,
            "created_at": utc_iso(strategy.created_at),
            "updated_at": utc_iso(strategy.updated_at),
        }
        if include_history:
            versions = list(
                db.scalars(
                    select(StrategyVersion)
                    .where(StrategyVersion.strategy_id == strategy.id)
                    .order_by(StrategyVersion.version.desc())
                ).all()
            )
            data["versions"] = [
                {
                    "id": v.id,
                    "version": v.version,
                    "parameters": v.parameters,
                    "entry_conditions": v.entry_conditions,
                    "exit_conditions": v.exit_conditions,
                    "risk_parameters": v.risk_parameters,
                    "code_hash": v.code_hash,
                    "change_note": v.change_note,
                    "created_at": utc_iso(v.created_at),
                }
                for v in versions
            ]
            runs = list(
                db.scalars(
                    select(StrategyRun)
                    .where(StrategyRun.strategy_id == strategy.id)
                    .order_by(StrategyRun.started_at.desc())
                    .limit(10)
                ).all()
            )
            data["recent_runs"] = [
                {
                    "id": r.id,
                    "strategy_version_id": r.strategy_version_id,
                    "started_at": utc_iso(r.started_at),
                    "ended_at": utc_iso(r.ended_at) if r.ended_at else None,
                    "status": r.status,
                    "signals_generated": r.signals_generated,
                    "summary": r.summary_json,
                }
                for r in runs
            ]
        return data


strategy_service = StrategyService()
