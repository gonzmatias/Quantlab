"""Deterministic robustness gates. No threshold tuning against validation results."""
import math

import numpy as np

VALIDATION_POLICY = {"version": "2", "mandatory": ["walk_forward", "parameters", "monte_carlo", "signal_delay"],
                     "diagnostic": ["regression", "concentration"]}


def validation_complete(quant, stress):
    return bool(quant.get("passed") and stress.get("passed")
                and quant.get("advanced_tests", {}).get("walk_forward", {}).get("passed")
                and all(stress.get("robustness_tests", {}).get(key, {}).get("passed") for key in
                        ("parameters", "monte_carlo", "signal_delay")))


def skipped(reason):
    return {"passed": False, "status": "SKIPPED", "reason": reason}


def regression_test(strategy_returns, market_returns, z_min=1.645, minimum=60):
    """OLS of net strategy returns on underlying returns, Newey-West covariance.

    Explanatory alpha/beta regression, not a predictive price model. Risk-free=0.
    One-sided asymptotic lower bound; does not correct adaptive research by itself.
    """
    y, market = np.asarray(strategy_returns, float), np.asarray(market_returns, float)
    failure = {"passed": False, "status": "FAILED", "reason": "Muestra insuficiente, no finita o regresión degenerada"}
    if len(y) != len(market) or len(y) < minimum or not np.isfinite(y).all() or not np.isfinite(market).all():
        return failure
    x = np.column_stack((np.ones(len(y)), market))
    if np.linalg.matrix_rank(x) < 2 or np.std(y) < 1e-12:
        return failure
    coef = np.linalg.lstsq(x, y, rcond=None)[0]
    residual = y - x @ coef
    scores = x * residual[:, None]
    lags = min(len(y) - 1, max(1, int(4 * (len(y) / 100) ** (2 / 9))))
    meat = scores.T @ scores
    for lag in range(1, lags + 1):
        cross = scores[lag:].T @ scores[:-lag]
        meat += (1 - lag / (lags + 1)) * (cross + cross.T)
    bread = np.linalg.inv(x.T @ x)
    covariance = bread @ meat @ bread * len(y) / (len(y) - 2)
    se = math.sqrt(max(0.0, float(covariance[0, 0])))
    if se < 1e-12 or not np.isfinite(covariance).all():
        return {**failure, "reason": "Error estándar del alfa degenerado; no se presume significancia"}
    lower = float(coef[0] - z_min * se)
    passed = lower > 0
    return {"passed": passed, "status": "PASSED" if passed else "FAILED", "observations": len(y),
            "alpha_per_bar": float(coef[0]), "beta": float(coef[1]), "alpha_standard_error": se,
            "alpha_z": float(coef[0] / se), "alpha_lower_bound": lower, "z_min": z_min,
            "r_squared": float(1 - residual @ residual / np.sum((y - y.mean()) ** 2)), "hac_lags": lags,
            "reason": "Alfa neto con límite inferior positivo" if passed else "Alfa no significativo frente al retorno del activo",
            "method": "OLS + Newey-West/Bartlett; risk-free=0; asymptotic one-sided bound"}


def summarize(result):
    return {k: result[k] for k in ("net_return", "sharpe", "max_drawdown", "trades", "bankrupt")}


def viable(result, cfg, min_trades=None):
    return (not result["bankrupt"] and result["net_return"] > 0 and result["sharpe"] > 0
            and result["max_drawdown"] <= cfg.max_drawdown
            and result["trades"] >= (cfg.min_trades if min_trades is None else min_trades))


def walk_forward_test(data, h, cfg, split, simulate, checkpoint):
    """Expanding training windows, purged gap, disjoint forward tests, frozen rules.

    This tests the exported fixed hypothesis, without selecting new parameters in
    the test windows. Each simulation starts flat and liquidates with costs.
    """
    boundaries = np.linspace(split, len(data), cfg.walk_forward_folds + 1, dtype=int)
    gap = h["max_holding"]
    rows, joined = [], []
    fold_min = max(1, math.ceil(cfg.min_trades / cfg.walk_forward_folds))
    for start, end in zip(boundaries[:-1], boundaries[1:]):
        checkpoint()
        train_end = int(start) - gap
        if train_end <= h["slow"] + 60 or end - start < 30:
            return {"passed": False, "status": "FAILED", "reason": "Historia insuficiente para walk-forward con separación temporal", "folds": rows}
        training = simulate(data.iloc[:train_end], h, cfg, 10000, h["slow"], train_end, checkpoint=checkpoint)
        testing = simulate(data.iloc[:int(end)], h, cfg, 10000, int(start), int(end), checkpoint=checkpoint)
        passed = viable(training, cfg) and viable(testing, cfg, fold_min)
        rows.append({"train_start": str(data.timestamp.iloc[h["slow"]]), "train_end": str(data.timestamp.iloc[train_end-1]),
                     "test_start": str(data.timestamp.iloc[int(start)]), "test_end": str(data.timestamp.iloc[int(end)-1]),
                     "gap_bars": gap, "passed": passed, "training": summarize(training), "testing": summarize(testing)})
        joined.extend(testing["returns"])
    returns = np.asarray(joined)
    curve = np.r_[1.0, np.cumprod(1 + returns)]
    dd = float(np.max(1 - curve / np.maximum.accumulate(curve)))
    required = math.ceil(2 * cfg.walk_forward_folds / 3)
    passed = (sum(row["passed"] for row in rows) >= required and curve[-1] > 1 and dd <= cfg.max_drawdown
              and sum(row["testing"]["trades"] for row in rows) >= cfg.min_trades
              and all(not row["testing"]["bankrupt"] and row["testing"]["max_drawdown"] <= cfg.max_drawdown for row in rows))
    return {"passed": bool(passed), "status": "PASSED" if passed else "FAILED", "folds": rows,
            "required_passing_folds": required, "passing_folds": sum(row["passed"] for row in rows),
            "fold_min_trades": fold_min, "net_return": float(curve[-1] - 1), "max_drawdown": dd,
            "method": "Expanding train / disjoint forward tests; frozen rules; gap=max_holding; no reoptimization",
            "reason": "Walk-forward estable" if passed else "Walk-forward insuficiente en estabilidad, operaciones, retorno o drawdown"}


def parameter_variants(h):
    """One-at-a-time ±20% shocks, schema bounds, only executable parameters."""
    if h.get("program"):
        from copy import deepcopy
        from strategy_rules import RuleProgram
        for index, parameter in enumerate(h["program"]["parameters"]):
            seen = set()
            for factor in (.8, 1.2):
                delta = max(abs(parameter["value"]) * .2, 1 if parameter["integer"] else (parameter["upper"] - parameter["lower"]) * .1)
                value = parameter["value"] + (-delta if factor < 1 else delta)
                value = min(parameter["upper"], max(parameter["lower"], value))
                value = int(round(value)) if parameter["integer"] else round(value, 8)
                if value == parameter["value"] or value in seen:
                    continue
                seen.add(value)
                candidate = deepcopy(h)
                candidate["program"]["parameters"][index]["value"] = value
                try:
                    RuleProgram.model_validate(candidate["program"])
                except ValueError:
                    continue
                key = "rule:" + parameter["name"]
                candidate[key] = value
                yield key, factor, candidate
        for key in ("allocation", "stop_loss", "take_profit", "max_holding"):
            for factor in (.8, 1.2):
                value = h[key] * factor
                if key == "max_holding":
                    value = min(10000, max(1, int(round(value))))
                elif key in ("allocation", "stop_loss"):
                    value = min(1 if key == "allocation" else .999999, value)
                if value != h[key]:
                    yield key, factor, {**h, key: value}
        return
    bounds = {"fast": (2, 40), "slow": (10, 150), "threshold": (.1, 3), "allocation": (.01, .95),
              "stop_loss": (.005, .15), "take_profit": (.01, .4), "max_holding": (2, 100)}
    inactive = set()
    if h["family"] in ("trend", "ema_trend", "dip", "breakout"):
        inactive.add("threshold")
    if h["family"] in ("breakout", "momentum", "mean_reversion", "volatility_breakout") and h.get("regime") != "low_volatility":
        inactive.add("fast")
    seen = set()
    for key, (low, high) in bounds.items():
        if key in inactive:
            continue
        for factor in (.8, 1.2):
            value = min(high, max(low, h[key] * factor))
            value = int(round(value)) if key in ("fast", "slow", "max_holding") else round(value, 8)
            candidate = {**h, key: value}
            if value == h[key] or (key, value) in seen or candidate["fast"] >= candidate["slow"]:
                continue
            seen.add((key, value))
            yield key, factor, candidate


def parameter_stress(data, h, cfg, start, simulate, checkpoint):
    rows = []
    for key, factor, variant in parameter_variants(h):
        checkpoint()
        result = simulate(data, variant, cfg, 100, start, checkpoint=checkpoint)
        rows.append({"parameter": key, "factor": factor, "value": variant[key],
                     "passed": bool(viable(result, cfg)), "metrics": summarize(result)})
    fraction = sum(row["passed"] for row in rows) / len(rows) if rows else 0.0
    passed = fraction >= cfg.parameter_pass_min and len(rows) >= 4
    return {"passed": passed, "status": "PASSED" if passed else "FAILED", "scenarios": rows,
            "passing_fraction": fraction, "required_fraction": cfg.parameter_pass_min,
            "reason": "Parámetros robustos" if passed else "Rendimiento frágil ante variaciones de parámetros"}


def monte_carlo_test(returns, cfg, checkpoint):
    """Circular block resampling of net daily portfolio returns, not price paths."""
    r = np.asarray(returns, float)
    if len(r) < 60 or not np.isfinite(r).all() or np.std(r) < 1e-12 or np.any(r <= -1):
        return {"passed": False, "status": "FAILED", "reason": "Monte Carlo: retornos insuficientes, degenerados o quiebra"}
    rng = np.random.default_rng(cfg.seed)
    rows = []
    for block in (5, 10, 20):
        profits, drawdowns, ruins = [], [], []
        for index in range(cfg.monte_carlo_samples):
            if index % 32 == 0:
                checkpoint()
            starts = rng.integers(0, len(r), size=math.ceil(len(r) / block))
            indices = ((starts[:, None] + np.arange(block)) % len(r)).ravel()[:len(r)]
            curve = np.r_[1.0, np.cumprod(1 + r[indices])]
            if not np.isfinite(curve).all():
                return {"passed": False, "status": "FAILED", "reason": "Monte Carlo produjo una trayectoria no finita"}
            profits.append(float(curve[-1] - 1))
            drawdowns.append(float(np.max(1 - curve / np.maximum.accumulate(curve))))
            # Operational ruin: loss of at least 95% of starting equity.
            ruins.append(bool(curve.min() <= .05))
        probability = float(np.mean(np.asarray(profits) > 0))
        dd95 = float(np.quantile(drawdowns, .95))
        ruin_probability = float(np.mean(ruins))
        passed = probability >= cfg.monte_carlo_profit_min and dd95 <= cfg.max_drawdown and ruin_probability <= .01
        rows.append({"block_bars": block, "paths": cfg.monte_carlo_samples, "profit_probability": probability,
                     "return_p05": float(np.quantile(profits, .05)), "return_p50": float(np.median(profits)),
                     "drawdown_p95": dd95, "ruin_probability": ruin_probability, "passed": passed})
    passed = all(row["passed"] for row in rows)
    return {"passed": passed, "status": "PASSED" if passed else "FAILED", "scenarios": rows, "seed": cfg.seed,
            "required_profit_probability": cfg.monte_carlo_profit_min, "max_ruin_probability": .01,
            "reason": "Monte Carlo aprobado" if passed else "Monte Carlo: probabilidad de pérdida o drawdown excesivo",
            "method": "Circular block bootstrap of net portfolio returns; 5/10/20 bars; conditional on historical sample"}


def concentration_test(returns):
    r = np.asarray(returns, float)
    if len(r) < 60 or not np.isfinite(r).all() or np.any(r <= -1):
        return {"passed": False, "status": "FAILED", "reason": "Muestra inválida para concentración"}
    adjusted = r.copy()
    positive = np.flatnonzero(r > 0)
    best = positive[np.argsort(r[positive])[-5:]]
    adjusted[best] = 0
    net = float(np.prod(1 + adjusted) - 1)
    passed = math.isfinite(net) and net > 0
    return {"passed": passed, "status": "PASSED" if passed else "FAILED", "removed_days": len(best),
            "net_return_without_best_days": net if math.isfinite(net) else None,
            "reason": "Retorno positivo sin los cinco mejores días" if passed else "El beneficio depende de los cinco mejores días",
            "method": "Zero best positive daily portfolio returns; sensitivity diagnostic, not a new execution backtest"}
