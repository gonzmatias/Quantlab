"""Exploratory diagnostics. Never interpreted as independent confirmation."""
import hashlib

import numpy as np
import pandas as pd

from execution import scheduled_signals, TIMEFRAMES, bar_seconds


def preliminary_experiment(data, h, signal_fn, program_fn):
    """Training only: conditional future returns; overlapping labels are disclosed."""
    entry, exits, decisions, _ = scheduled_signals(data, h, signal_fn, program_fn)
    rows = []
    scale = TIMEFRAMES[h.get("timeframe", "1d")] // bar_seconds(data)
    for horizon in (1, 5, 20):
        forward = data.open.shift(-1-horizon*scale) / data.open.shift(-1) - 1
        if h.get("direction", "long") == "short":
            forward = -forward
        selected = forward[entry & decisions].dropna()
        reference = forward[decisions].dropna()
        rows.append({"signal_bars": horizon, "events": len(selected),
                     "conditional_mean": float(selected.mean()) if len(selected) else None,
                     "unconditional_mean": float(reference.mean()) if len(reference) else None})
    return {"scope": "TRAINING_ONLY", "horizons": rows,
            "signal_fingerprint": hashlib.sha256(np.column_stack((entry, ~entry if exits is None else exits, decisions)).astype("uint8").tobytes()).hexdigest(),
            "limitation": "Gross overlapping forward labels; no significance, costs or independent evidence"}


def regime_diagnostics(data, returns, start):
    """Classifications use only preceding prices; thresholds fitted on training."""
    r = data.close.pct_change()
    volatility = r.rolling(20, min_periods=20).std().shift(1)
    threshold = volatility.iloc[:start].median()
    trend = data.close.pct_change(20).shift(1)
    values = np.asarray(returns, dtype=float)
    labels = {"high_volatility": volatility > threshold, "low_volatility": volatility <= threshold,
              "uptrend": trend > 0, "downtrend": trend < 0}
    rows = {}
    for name, mask in labels.items():
        sample = values[mask.iloc[start:start+len(values)].to_numpy()]
        rows[name] = {"bars": len(sample), "mean_return": float(sample.mean()) if len(sample) else None,
                      "compounded_return": float(np.prod(1+sample)-1) if len(sample) else None}
    return {"status": "DIAGNOSTIC", "regimes": rows,
            "limitation": "Regimes overlap; regrouped returns are not a tradable equity curve"}


def placebo_diagnostics(data, h, cfg, start, simulate, signal_fn, program_fn, checkpoint):
    """Randomly shifted decision schedules, actual costs and risk rules reapplied.

    Event counts are preserved before segment boundaries. Stops/rounding may change
    realized exposure and trade counts, which are reported rather than asserted equal.
    """
    if not cfg.placebo_samples:
        return {"status": "DISABLED"}
    if TIMEFRAMES[h.get("timeframe", "1d")] != bar_seconds(data):
        return {"status": "UNAVAILABLE", "reason": "Comparador multirresolución pendiente: no se altera la cadencia de stops para fabricar equivalencia"}
    entry, exits, _, _ = scheduled_signals(data, h, signal_fn, program_fn)
    if exits is None:
        exits = ~entry
    rng, rows = np.random.default_rng(cfg.seed), []
    n = len(data)-start
    if n < 30:
        return {"status": "INSUFFICIENT_DATA"}
    for _ in range(cfg.placebo_samples):
        checkpoint()
        shift = int(rng.integers(1, n))
        frame = data.copy()
        frame["placebo_entry"], frame["placebo_exit"] = 0., 0.
        frame.loc[frame.index[start:], "placebo_entry"] = np.roll(entry[start:], shift).astype(float)
        frame.loc[frame.index[start:], "placebo_exit"] = np.roll(exits[start:], shift).astype(float)
        # Schedules are already on execution bars, including sparse decision events.
        rule = {**h, "timeframe": next(k for k, v in TIMEFRAMES.items() if v == bar_seconds(data)),
                "max_holding": h["max_holding"] * (TIMEFRAMES[h.get("timeframe", "1d")]//bar_seconds(data)),
                "program": {"entry": "col('placebo_entry') > 0", "exit": "col('placebo_exit') > 0", "parameters": []}}
        result = simulate(frame, rule, cfg, cfg.capital, start, checkpoint=checkpoint)
        rows.append({k: result.get(k) for k in ("net_return", "sharpe", "trades", "exposure", "turnover")})
    return {"status": "DIAGNOSTIC", "samples": rows,
            "limitation": "Development-only random schedules; realized exposure may differ; not a calibrated p-value"}
