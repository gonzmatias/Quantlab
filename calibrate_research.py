"""Controlled calibration of discovery gates, not of the web/LLM generator.

Runs predeclared candidate rules on independent null and planted-effect worlds.
Never consumes a production holdout and never exports an approved strategy.
"""
import argparse
import json
import tempfile
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from trading_agent import TradingAgent, Settings, initial_state


def controlled_market(seed, effect=0., bars=2200):
    rng = np.random.default_rng(seed)
    feature = rng.choice([-1., 1.], size=bars)
    innovations = rng.normal(0, .006, bars)
    innovations[2:] += effect * feature[:-2]
    price = 100 * np.exp(np.cumsum(innovations))
    return pd.DataFrame({"timestamp": pd.date_range("2010-01-01", periods=bars, freq="B", tz="UTC"),
                         "open": price, "close": price, "high": price*1.001, "low": price*.999,
                         "synthetic_feature": feature})


def controlled_rule():
    return {"family": "controlled effect", "rationale": "Predeclared synthetic lagged feature, for calibration only",
            "direction": "long", "timeframe": "1d", "fast": 2, "slow": 10, "threshold": 1.,
            "allocation": .5, "stop_loss": .1, "take_profit": .2, "max_holding": 1,
            "program": {"entry": "col('synthetic_feature') > threshold", "exit": "col('synthetic_feature') < threshold",
                        "parameters": [{"name": "threshold", "value": 0., "lower": -1., "upper": 1., "integer": False}]}}


def calibrate(repetitions=10, cfg=None, hypotheses=None):
    if not 1 <= repetitions <= 1000:
        raise ValueError("Repeticiones entre 1 y 1000")
    cfg = cfg or Settings()
    hypotheses = hypotheses or [controlled_rule()]
    rows = []
    with tempfile.TemporaryDirectory(prefix="quantlab_calibration_") as folder:
        for world, effect in (("null", 0.), ("planted", .003)):
            for repetition in range(repetitions):
                # Different worlds use disjoint seeds; each search charges all candidates.
                seed = cfg.seed + repetition + (100000 if effect else 0)
                data = controlled_market(seed, effect)
                agent = TradingAgent(data, cfg, Path(folder)/f"{world}_{repetition}", demo=True)
                outcomes = []
                for index, h in enumerate(hypotheses, 1):
                    agent.quant_trial_index = len(hypotheses)
                    state = {**initial_state(), "iteration_count": index, "hypothesis": h}
                    state.update(agent._quant_single(state))
                    if state["quant_metrics"]["passed"]:
                        state.update(agent._stress_single(state))
                    passed = bool(state["quant_metrics"]["passed"] and state.get("stress_metrics", {}).get("passed"))
                    outcomes.append({"index": index, "passed": passed,
                                     "reasons": state["quant_metrics"].get("rejection_reasons", []) + state.get("stress_metrics", {}).get("rejection_reasons", [])})
                    if passed:
                        break
                rows.append({"world": world, "seed": seed, "selected": any(x["passed"] for x in outcomes), "trials": outcomes})
    rates = {world: sum(r["selected"] for r in rows if r["world"] == world)/repetitions for world in ("null", "planted")}
    return {"settings": asdict(cfg), "hypotheses": hypotheses, "repetitions_per_world": repetitions,
            "empirical_selection_rates": rates, "experiments": rows,
            "limitation": "Synthetic discovery-gate calibration; small-sample rates, not a guarantee or calibration of the adaptive LLM/full production pipeline"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--hypotheses", type=Path, help="Predeclared JSON array of hypotheses")
    parser.add_argument("--output", type=Path, default=Path("calibration_report.json"))
    args = parser.parse_args()
    cfg = Settings(**json.loads(args.settings.read_text())) if args.settings else Settings()
    hypotheses = json.loads(args.hypotheses.read_text()) if args.hypotheses else None
    result = calibrate(args.repetitions, cfg, hypotheses)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["empirical_selection_rates"], indent=2))
