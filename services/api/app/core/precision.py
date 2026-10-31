from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, InvalidOperation
from typing import Any


ZERO = Decimal("0")
ONE = Decimal("1")
BPS_DIVISOR = Decimal("10000")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    target = dt or utc_now()
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return target.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def to_decimal(value: Any, default: Decimal | None = None) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        if default is not None:
            return default
        raise ValueError("Cannot convert None/empty to Decimal")
    if isinstance(value, (int, str)):
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            if default is not None:
                return default
            raise ValueError(f"Invalid decimal value: {value!r}") from exc
    if isinstance(value, float):
        # Convert float via str to avoid binary floating point artifacts
        return Decimal(str(value))
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        if default is not None:
            return default
        raise ValueError(f"Invalid decimal value: {value!r}") from exc


def round_to_step(value: Decimal | str | int, step: Decimal | str | int, rounding=ROUND_DOWN) -> Decimal:
    val = to_decimal(value)
    st = to_decimal(step)
    if st <= ZERO:
        raise ValueError(f"Step must be positive, got {st}")
    units = (val / st).to_integral_value(rounding=rounding)
    result = (units * st).quantize(st.normalize(), rounding=rounding)
    return result


def round_price_to_tick(price: Decimal | str | int, tick_size: Decimal | str | int) -> Decimal:
    return round_to_step(price, tick_size, rounding=ROUND_HALF_UP)


def round_qty_to_lot(qty: Decimal | str | int, lot_size: Decimal | str | int) -> Decimal:
    return round_to_step(qty, lot_size, rounding=ROUND_DOWN)


def calc_spread_bps(bid: Decimal | str, ask: Decimal | str) -> Decimal:
    b = to_decimal(bid)
    a = to_decimal(ask)
    if b <= ZERO or a <= ZERO or a < b:
        return ZERO
    mid = (a + b) / Decimal("2")
    return ((a - b) / mid * BPS_DIVISOR).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calc_position_sizing(
    *,
    account_equity: Decimal | str,
    risk_rate: Decimal | str,
    entry_price: Decimal | str,
    stop_price: Decimal | str,
    market_type: str,
    lot_size: Decimal | str = Decimal("0.0001"),
    min_qty: Decimal | str = Decimal("0.0001"),
    min_notional: Decimal | str = Decimal("5.0"),
    contract_size: Decimal | str = ONE,
    fee_rate: Decimal | str = Decimal("0.0006"),
    slippage_bps: Decimal | str = Decimal("5"),
    leverage: Decimal | str = ONE,
) -> dict[str, Decimal]:
    """
    Deterministic position sizing per Section 3.7:
    risk_budget = account_equity * risk_rate
    raw_qty = risk_budget / (abs(entry_price - stop_price) + fee_and_slippage_per_unit)
    For USDT perpetual contracts with contract_size != 1, convert base asset quantity into contract count.
    """
    equity = to_decimal(account_equity)
    rate = to_decimal(risk_rate)
    entry = to_decimal(entry_price)
    stop = to_decimal(stop_price)
    lot = to_decimal(lot_size)
    min_q = to_decimal(min_qty)
    min_not = to_decimal(min_notional)
    c_size = to_decimal(contract_size)
    fee = to_decimal(fee_rate)
    slip_rate = to_decimal(slippage_bps) / BPS_DIVISOR
    lev = to_decimal(leverage)

    if equity <= ZERO:
        raise ValueError("Account equity must be positive")
    if rate <= ZERO or rate > Decimal("0.10"):
        raise ValueError("Risk rate out of safe bounds")
    if entry <= ZERO or stop <= ZERO:
        raise ValueError("Entry and stop prices must be positive")

    stop_distance = abs(entry - stop)
    if stop_distance <= ZERO:
        raise ValueError("Stop loss distance cannot be zero")

    risk_budget = (equity * rate).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    # Include round-trip fee + slippage buffer per unit of base asset
    cost_buffer_per_unit = entry * (fee * Decimal("2") + slip_rate)
    effective_unit_risk = stop_distance + cost_buffer_per_unit

    raw_base_qty = risk_budget / effective_unit_risk

    if market_type == "usdt_perp":
        if c_size <= ZERO:
            raise ValueError("Contract size must be positive")
        raw_instrument_qty = raw_base_qty / c_size
        rounded_qty = round_qty_to_lot(raw_instrument_qty, lot)
        base_qty = rounded_qty * c_size
    else:
        rounded_qty = round_qty_to_lot(raw_base_qty, lot)
        base_qty = rounded_qty

    notional = (base_qty * entry).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    effective_lev = lev if market_type == "usdt_perp" and lev > ZERO else ONE
    margin_required = (notional / effective_lev).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    estimated_fee = (notional * fee * Decimal("2")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    max_planned_loss = (base_qty * effective_unit_risk).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    return {
        "risk_budget": risk_budget,
        "stop_distance": stop_distance,
        "raw_base_qty": raw_base_qty.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN),
        "qty": rounded_qty,
        "base_qty": base_qty,
        "notional": notional,
        "margin_required": margin_required,
        "estimated_fee": estimated_fee,
        "max_planned_loss": max_planned_loss,
        "min_qty": min_q,
        "min_notional": min_not,
    }
