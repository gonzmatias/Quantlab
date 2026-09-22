"""Causal bar scheduling and explicit instrument capabilities. No invented ticks."""
import math

import numpy as np
import pandas as pd


TIMEFRAMES = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
              "1h": 3600, "4h": 14400, "1d": 86400}


def bar_seconds(data):
    if "timestamp" not in data or len(data) < 2:
        return 86400
    differences = data.timestamp.diff().dt.total_seconds().dropna()
    seconds = int(differences.mode().iloc[0])
    if seconds not in TIMEFRAMES.values():
        raise ValueError("Frecuencia de datos no soportada; se requieren barras UTC regulares")
    return seconds


def execution_capabilities(data, allow_short=False):
    seconds = bar_seconds(data)
    return {"execution_bar_seconds": seconds,
            "signal_timeframes": [name for name, value in TIMEFRAMES.items() if value >= seconds and value % seconds == 0],
            "directions": ["long", "short"] if allow_short else ["long"],
            "order_types": ["market"], "stops": "signal_close_next_open",
            "quote_model": "bid_ask" if {"bid_open", "ask_open", "bid_close", "ask_close"}.issubset(data.columns) else "spread_proxy",
            "tick_execution_verified": False}


def scheduled_signals(data, h, signal_fn, program_fn):
    """Return closed-bar decisions indexed by execution bar, never partial candles.

    Each decision is consumed at the next execution opening. Larger signal bars
    must contain every constituent bar: gaps disable decisions, never fill prices.
    """
    base = bar_seconds(data)
    target = TIMEFRAMES.get(h.get("timeframe", "1d"))
    if target is None or target < base or target % base:
        raise ValueError("NEEDS_CAPABILITY: datos demasiado gruesos para la temporalidad propuesta")
    if target == base:
        entry, exits = program_fn(data, h["program"]) if h.get("program") else (signal_fn(data, h), None)
        return entry, exits, np.ones(len(data), dtype=bool), data.close.to_numpy()
    frame = signal_frame(data, h.get("timeframe", "1d"))
    entry = np.zeros(len(data), dtype=bool)
    exits = np.zeros(len(data), dtype=bool)
    decisions = np.zeros(len(data), dtype=bool)
    prices = np.full(len(data), np.nan)
    if len(frame):
        en, ex = program_fn(frame, h["program"]) if h.get("program") else (signal_fn(frame, h), None)
        indices = data.timestamp.searchsorted(frame.timestamp + pd.Timedelta(seconds=target-base))
        entry[indices] = en
        exits[indices] = ~en if ex is None else ex
        decisions[indices] = True
        prices[indices] = frame.close
    return entry, exits, decisions, prices


def signal_frame(data, timeframe):
    base, target = bar_seconds(data), TIMEFRAMES.get(timeframe)
    if target is None or target < base or target % base:
        raise ValueError("NEEDS_CAPABILITY: temporalidad no construible con estos datos")
    if target == base:
        return data.copy()
    groups = data.timestamp.dt.floor(f"{target}s")
    aggregations = {name: "last" for name in data.columns if name != "timestamp"}
    aggregations.update(open="first", high="max", low="min", close="last")
    if "volume" in data:
        aggregations["volume"] = "sum"
    grouped = data.groupby(groups)
    frame = grouped.agg(aggregations)
    counts = grouped.size()
    last_times = grouped.timestamp.max()
    complete = (counts == target // base) & (last_times == frame.index + pd.Timedelta(seconds=target-base))
    frame = frame.loc[complete].reset_index()
    frame.columns = ["timestamp", *frame.columns[1:]]
    return frame


def causal_signal_audit(data, h, signal_fn, program_fn):
    """Prefix invariance: append and corrupt future data without changing decisions."""
    full = scheduled_signals(data, h, signal_fn, program_fn)
    checks = []
    for end in sorted({max(2, len(data)//2), max(2, len(data)*3//4)}):
        if end >= len(data):
            continue
        prefix = scheduled_signals(data.iloc[:end], h, signal_fn, program_fn)
        # Appending data must not complete an earlier, still-open signal candle.
        ok = all(np.array_equal(a[:end], b, equal_nan=True) for a, b in zip(full, prefix) if a is not None and b is not None)
        altered = data.copy()
        numeric = [name for name in altered if name != "timestamp" and pd.api.types.is_numeric_dtype(altered[name])]
        altered[numeric] = altered[numeric].astype(float)
        altered.loc[altered.index[end:], numeric] *= 1.731
        changed = scheduled_signals(altered, h, signal_fn, program_fn)
        future_ok = all(np.array_equal(a[:end], b[:end], equal_nan=True) for a, b in zip(full, changed) if a is not None and b is not None)
        checks.append({"prefix_bars": end, "passed": bool(ok and future_ok), "future_mutation_invariant": bool(future_ok)})
    return {"passed": bool(checks) and all(c["passed"] for c in checks),
            "checks": checks, "method": "prefix invariance of signal availability and values",
            "limitation": "Does not detect revised source vintages or historical knowledge in the model"}
