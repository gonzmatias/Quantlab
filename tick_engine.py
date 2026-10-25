"""Deterministic bid/ask event replay. Quote-crossing model, not exchange queue simulation."""
import math

import numpy as np
import pandas as pd

from execution import TIMEFRAMES
from strategy_rules import program_signals
from tick_data import RETURN_SECONDS, TICK_MODEL, TickError, TickExecution, utc, validate_ticks


def tick_signals(ticks, h, execution):
    n = len(ticks)
    if h["timeframe"] == "tick":
        frame = ticks[["bid", "ask"]].copy()
        frame["mid"] = (frame.bid + frame.ask) / 2
        en, ex = program_signals(frame, h["program"])
        return en, ex, np.ones(n, dtype=bool)
    seconds = TIMEFRAMES.get(h["timeframe"])
    if not seconds:
        raise TickError("NEEDS_CAPABILITY", "Temporalidad de señal no disponible")
    price = ((ticks.bid + ticks.ask) / 2 if execution.signal_price == "mid" else ticks[execution.signal_price])
    groups = ticks.timestamp.dt.floor(f"{seconds}s")
    bars = pd.DataFrame({"timestamp": groups, "price": price}).groupby("timestamp").price.agg(
        open="first", high="max", low="min", close="last").reset_index()
    # A first partial bucket is not a complete candle. Require coverage from its boundary.
    bars = bars[bars.timestamp >= ticks.timestamp.iloc[0]].reset_index(drop=True)
    en, ex, decisions = np.zeros(n, bool), np.zeros(n, bool), np.zeros(n, bool)
    if bars.empty:
        return en, ex, decisions
    entry, exits = program_signals(bars, h["program"])
    for j, close in enumerate(bars.timestamp + pd.Timedelta(seconds=seconds)):
        i = int(ticks.timestamp.searchsorted(close))
        if i < n:
            en[i], ex[i], decisions[i] = entry[j], exits[j], True
    return en, ex, decisions


def distance(price, value, execution):
    return price * value if execution.distance_unit == "percent" else value * execution.pip_size if execution.distance_unit == "pips" else value


def replay_ticks(ticks, h, cfg, execution, start=None, end=None, cost_multiplier=1.0,
                 checkpoint=lambda: None, signal_delay_ms=0, benchmark=False):
    execution = TickExecution.model_validate(execution)
    ticks = validate_ticks(ticks)
    if h["direction"] not in ("long", "short") or (h["direction"] == "short" and not cfg.allow_short):
        raise TickError("NEEDS_CAPABILITY", "Dirección no negociable")
    if not math.isfinite(cost_multiplier) or cost_multiplier <= 0 or signal_delay_ms < 0:
        raise ValueError("Multiplicador o retraso inválido")
    gap = pd.Timedelta(milliseconds=execution.max_quote_gap_ms)
    start = utc(start) if start is not None else ticks.timestamp.iloc[0]
    end = utc(end) if end is not None else ticks.timestamp.iloc[-1] + pd.Timedelta(nanoseconds=1)
    if start >= end:
        raise ValueError("Intervalo vacío")
    # No access to events at or beyond the end, including for indicators.
    ticks = ticks[ticks.timestamp < end].reset_index(drop=True)
    first = int(ticks.timestamp.searchsorted(start))
    if first >= len(ticks) or len(ticks) - first < 2:
        raise TickError("INSUFFICIENT_DATA", "Se requieren dos ticks en el segmento")
    if (ticks.timestamp.diff().dropna() > gap).any() or ticks.timestamp.iloc[first] - start > gap or end - ticks.timestamp.iloc[-1] > gap:
        raise TickError("NEEDS_DATA", "Hueco de cotizaciones supera max_quote_gap_ms")
    entry_signals, exit_signals, decisions = tick_signals(ticks, h, execution)
    direction = -1 if h["direction"] == "short" else 1
    if benchmark:
        direction = 1
        entry_signals = np.zeros(len(ticks), bool)
        entry_signals[first] = True
        exit_signals = np.zeros(len(ticks), bool)
    rate, fixed = cfg.commission_rate * cost_multiplier, cfg.fixed_commission * cost_multiplier
    slip = cfg.slippage_bps * cost_multiplier / 10000
    if rate >= 1 or slip >= 1:
        raise ValueError("Costos de estrés excesivos")
    times, seq = ticks.timestamp.tolist(), ticks.sequence.to_numpy()
    mid = (ticks.bid.to_numpy() + ticks.ask.to_numpy()) / 2
    half = (ticks.ask.to_numpy() - ticks.bid.to_numpy()) / 2 * cost_multiplier
    bids, asks = mid - half, mid + half
    if (bids <= 0).any():
        raise ValueError("Spread de estrés genera precio no positivo")
    cash, qty, entry_price, fees = cfg.capital, 0., 0., 0.
    entry_index, age, stop, target = 0, 0, 0., 0.
    pending_entry, pending_exit = None, None
    trade_log, order_log, marks, mark_times = [], [], [], []
    skipped, exposure_ns, turnover = 0, 0, 0.
    peak, drawdown, bankrupt = cfg.capital, 0., False

    def price(i, side):
        return float(asks[i] * (1 + slip) if side > 0 else bids[i] * (1 - slip))

    def event(kind, i, **details):
        order_log.append({"event": kind, "timestamp": str(times[i]), "sequence": int(seq[i]), **details})

    def close(i, reason):
        nonlocal cash, qty, turnover, pending_exit, pending_entry
        fill = price(i, -direction)
        commission = qty * fill * rate + fixed
        pnl = direction * qty * (fill-entry_price) - fees - commission
        cash += direction * qty * fill - commission
        turnover += qty * fill
        trade_log.append({"entry_timestamp": str(times[entry_index]), "exit_timestamp": str(times[i]),
                          "entry_sequence": int(seq[entry_index]), "exit_sequence": int(seq[i]),
                          "direction": "long" if direction > 0 else "short", "quantity": float(qty),
                          "entry_price": float(entry_price), "exit_price": fill, "pnl": float(pnl),
                          "exit_reason": reason})
        event("exit_fill", i, reason=reason, price=fill)
        qty, pending_exit, pending_entry = 0., None, None

    def protective(i):
        nonlocal pending_exit
        if not qty or pending_exit or benchmark:
            return False
        liquid = bids[i] if direction > 0 else asks[i]
        reason = "stop" if direction * (liquid-stop) <= 0 else "target" if direction * (liquid-target) >= 0 else None
        if reason == "target" and execution.target_order == "limit":
            if direction * (price(i, -direction)-target) >= 0:
                close(i, "target_limit")
                return True
        elif reason:
            pending_exit = {"index": i, "ready": times[i] + pd.Timedelta(milliseconds=execution.latency_ms), "reason": reason}
            event("exit_trigger", i, reason=reason)
        return False

    for i in range(first, len(ticks)):
        if (i-first) % 1024 == 0:
            checkpoint()
        acted = False
        if qty:
            exposure_ns += times[i].value - times[i-1].value
            elapsed_days = (times[i]-times[i-1]).total_seconds() / 86400
            carry = qty * mid[i-1] * cfg.holding_cost_bps / 10000 * cost_multiplier * elapsed_days
            cash -= carry
            fees += carry
            age += int(decisions[i])
        if pending_entry and (times[i] >= pending_entry["expires"] or exit_signals[i]):
            event("entry_cancel", i, reason="expired" if times[i] >= pending_entry["expires"] else "exit_signal")
            pending_entry = None
            acted = True
        if qty and pending_exit and i > pending_exit["index"] and times[i] >= pending_exit["ready"]:
            close(i, pending_exit["reason"])
            acted = True
        if qty:
            acted = protective(i) or acted
        if pending_entry and i > pending_entry["index"] and times[i] >= pending_entry["ready"]:
            fill = price(i, direction)
            eligible = pending_entry["limit"] is None or direction * (fill-pending_entry["limit"]) <= 0
            if eligible:
                units = math.floor(max(0, (cash * h["allocation"] - fixed)/(fill*(1+rate))) / cfg.quantity_step) * cfg.quantity_step
                if units > 0 and units * fill >= cfg.min_notional:
                    qty, entry_price, entry_index, age = units, fill, i, 0
                    fees = qty * fill * rate + fixed
                    cash -= direction * qty * fill + fees
                    turnover += qty * fill
                    stop = fill - direction * distance(fill, h["stop_loss"], execution)
                    target = fill + direction * distance(fill, h["take_profit"], execution)
                    if not benchmark and (stop <= 0 or target <= 0):
                        raise TickError("INVALID_SPEC", "Stop/objetivo resulta en precio no positivo")
                    event("entry_fill", i, price=fill, quantity=float(qty), stop=float(stop), target=float(target))
                    pending_entry = None
                    acted = protective(i) or acted
                else:
                    skipped += 1
                    event("entry_rejected", i, reason="capital_or_minimum")
                    pending_entry = None
                    acted = True
        if qty and not pending_exit and not benchmark:
            expired = ((times[i]-times[entry_index]).total_seconds()*1000 >= h["max_holding"]
                       if execution.holding_unit == "milliseconds" else age >= h["max_holding"])
            if exit_signals[i] or expired:
                pending_exit = {"index": i, "ready": times[i] + pd.Timedelta(milliseconds=execution.latency_ms + (signal_delay_ms if exit_signals[i] else 0)),
                                "reason": "condition" if exit_signals[i] else "max_holding"}
                event("exit_trigger", i, reason=pending_exit["reason"])
        if not qty and not pending_entry and not acted and entry_signals[i] and not exit_signals[i] and cash > 0:
            reference = asks[i] if direction > 0 else bids[i]
            limit = (reference - direction * distance(reference, execution.entry_limit_offset, execution)
                     if execution.entry_order == "limit" and not benchmark else None)
            if limit is not None and limit <= 0:
                raise TickError("INVALID_SPEC", "Límite de entrada no positivo")
            pending_entry = {"index": i, "ready": times[i] + pd.Timedelta(milliseconds=execution.latency_ms + signal_delay_ms),
                             "expires": times[i] + pd.Timedelta(milliseconds=execution.order_ttl_ms), "limit": limit}
            event("entry_submit", i, limit=None if limit is None else float(limit))
        value = cash + direction * qty * price(i, -direction) - (qty * price(i,-direction)*rate + fixed if qty else 0)
        peak = max(peak, value)
        drawdown = max(drawdown, 1-value/peak)
        if value <= 0:
            if qty: close(i, "insolvency")
            value, bankrupt = cash, True
        marks.append(float(value))
        mark_times.append(times[i])
        if bankrupt:
            break
    last = first + len(marks) - 1
    if qty:
        close(last, "segment_end")
        marks[-1] = float(cash)
    if pending_entry:
        event("entry_cancel", last, reason="segment_end")
    seconds = RETURN_SECONDS[execution.return_interval]
    grid_start = start.floor(f"{seconds}s") + pd.Timedelta(seconds=seconds)
    grid_end = end.ceil(f"{seconds}s")
    grid = pd.date_range(grid_start, grid_end, freq=f"{seconds}s")
    # Left limit at each boundary: a tick exactly on the boundary belongs to the next interval.
    ns = np.array([t.value for t in mark_times], dtype=np.int64)
    positions = np.searchsorted(ns, grid.as_unit("ns").asi8, side="left") - 1
    sampled = np.array([cfg.capital if i < 0 else marks[i] for i in positions])
    curve = np.r_[cfg.capital, sampled]
    returns = np.diff(curve) / np.maximum(curve[:-1], 1e-12)
    sd = float(returns.std(ddof=1)) if len(returns)>1 else 0.
    profits = [t["pnl"] for t in trade_log]
    gains, losses = sum(max(p,0) for p in profits), -sum(min(p,0) for p in profits)
    return {"execution_model": TICK_MODEL, "returns": returns, "equity": curve,
            "return_timestamps": [str(t) for t in grid], "return_interval": execution.return_interval,
            "net_return": float(cash/cfg.capital-1), "final_equity": float(cash),
            "sharpe": float(returns.mean()/sd*math.sqrt(cfg.bars_per_year)) if sd>1e-12 else 0.,
            "max_drawdown": float(max(0,drawdown)), "bankrupt": bankrupt,
            "trades": len(trade_log), "trade_log": trade_log, "order_log": order_log,
            "profit_factor": gains/losses if losses else None, "no_losing_trades": bool(gains and not losses),
            "skipped_orders": skipped, "exposure": exposure_ns/(end.value-start.value), "turnover": turnover/cfg.capital,
            "expectancy": float(np.mean(profits)) if profits else 0., "hit_rate": float(np.mean(np.array(profits)>0)) if profits else 0.}
