"""Frozen historical holdout and forward evaluation, independent of the researcher."""
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict

import pandas as pd


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def partition(datasets):
    start = max(f.timestamp.iloc[0] for f in datasets.values())
    end = min(f.timestamp.iloc[-1] for f in datasets.values())
    cutoff = start + (end - start) * .8
    training_end = start + (cutoff - start) * .75
    discovery = {a: f[f.timestamp < cutoff].reset_index(drop=True) for a, f in datasets.items()}
    if any(len(f) < 600 for f in discovery.values()):
        raise ValueError("Se requieren al menos 600 barras de descubrimiento antes de reservar el 20% final")
    if any((f.timestamp >= cutoff).sum() < 120 for f in datasets.values()):
        raise ValueError("La reserva final necesita al menos 120 barras por activo")
    return discovery, training_end, cutoff


class HoldoutLedger:
    """Consume the time interval globally, before evaluation; crashes never refund it.

    Deliberately independent of data hashes, settings, strategy and asset: changing
    a cost or trying a correlated market must not reset the final experiment.
    """
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS holdouts (id INTEGER PRIMARY KEY, start TEXT, end TEXT, run TEXT, signature TEXT)")

    def latest_end(self):
        with closing(sqlite3.connect(self.path)) as db:
            value = db.execute("SELECT MAX(end) FROM holdouts").fetchone()[0]
        return pd.Timestamp(value) if value else None

    def reserve(self, start, end, run, signature):
        start, end = pd.Timestamp(start).isoformat(), pd.Timestamp(end).isoformat()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM holdouts WHERE start <= ? AND end >= ?", (end, start)).fetchone():
                return False
            db.execute("INSERT INTO holdouts(start,end,run,signature) VALUES (?,?,?,?)", (start, end, run, signature))
        return True


def benchmark_test(data, h, cfg, start, simulate):
    # Same allocation, fees, financing and rounding; no discretionary exits.
    baseline = {**h, "program": {"entry": "True", "exit": "False", "parameters": []},
                "stop_loss": 1.0, "take_profit": 1e100, "max_holding": len(data) + 1}
    strategy = simulate(data, h, cfg, cfg.capital, start)
    passive = simulate(data, baseline, cfg, cfg.capital, start)
    # Predeclared risk-adjusted improvement, with a lower-risk alternative path.
    passed = (strategy["net_return"] > 0 and strategy["trades"] > 0 and
              strategy["sharpe"] > passive["sharpe"] and
              (strategy["net_return"] > passive["net_return"] or
               strategy["max_drawdown"] < passive["max_drawdown"]))
    keys = ("net_return", "sharpe", "max_drawdown", "trades", "final_equity")
    return {"passed": bool(passed), "status": "PASSED" if passed else "FAILED",
            "strategy": {k: strategy[k] for k in keys}, "buy_and_hold": {k: passive[k] for k in keys},
            "cash_return": 0.0,
            "reason": "Ventaja frente a efectivo y comprar/mantener" if passed else "No mejora el Sharpe y retorno o drawdown de comprar/mantener",
            "method": "Mismo capital y asignación; costos y mínimos incluidos; efectivo con interés cero; criterio económico, no significancia estadística"}


def evaluate_frozen(data, h, cfg, start, checkpoint=lambda: None):
    from trading_agent import backtest, compact
    from validation import viable, monte_carlo_test
    simulate = lambda *args, **kwargs: backtest(*args, **kwargs, checkpoint=checkpoint)
    if len(data) - start < 120:
        return {"passed": False, "status": "INSUFFICIENT_DATA", "reason": "Se requieren 120 barras nuevas"}
    results = {str(mult): simulate(data, h, cfg, cfg.capital, start, cost_multiplier=mult) for mult in (1, 2, 3)}
    benchmark = benchmark_test(data, h, cfg, start, simulate)
    mc = monte_carlo_test(results["1"]["returns"], cfg, checkpoint)
    delayed = simulate(data, h, cfg, cfg.capital, start, signal_delay=1)
    passed = (all(viable(r, cfg, cfg.final_min_trades) for r in results.values()) and benchmark["passed"]
              and mc["passed"] and viable(delayed, cfg, cfg.final_min_trades))
    return {"passed": bool(passed), "status": "PASSED" if passed else "FAILED",
            "start": str(data.timestamp.iloc[start]), "end": str(data.timestamp.iloc[-1]),
            "bars": len(data)-start, "scenarios": {k: compact(v) for k, v in results.items()},
            "benchmark": benchmark, "monte_carlo": mc, "signal_delay": compact(delayed),
            "reason": "Reserva histórica superada; validación prospectiva pendiente" if passed else "La estrategia congelada no superó la reserva final; no se reformula con este resultado"}


def frozen_candidate(h, cfg, asset, cutoff, last_timestamp):
    payload = {"version": 1, "hypothesis": h, "settings": asdict(cfg), "asset": asset,
               "holdout_start": str(cutoff), "observed_through": str(last_timestamp),
               "frozen_at": pd.Timestamp.now(tz="UTC").isoformat(),
               "execution_validation": "PENDING_MT5_AND_BROKER", "forward_validation": "PENDING"}
    return {**payload, "sha256": digest(payload)}
