import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from trading_agent import Settings, TradingAgent, Hypothesis, backtest, demo_data, initial_state, variant_hypothesis
from validation import (regression_test, walk_forward_test, parameter_variants, parameter_stress,
                        monte_carlo_test, concentration_test)
from report_agent import build_report, render_html


def successful(n=120):
    return dict(bankrupt=False, net_return=.1, max_drawdown=.01, sharpe=2., trades=40,
                returns=np.linspace(.0001, .0003, n))


class ValidationTests(unittest.TestCase):
    def test_regression_detects_alpha_and_rejects_beta_only(self):
        rng = np.random.default_rng(41)
        market = rng.normal(.0002, .01, 1500)
        noise = rng.normal(0, .001, len(market))
        result = regression_test(.001 + .4 * market + noise, market)
        self.assertTrue(result["passed"])
        self.assertAlmostEqual(result["beta"], .4, delta=.02)
        self.assertAlmostEqual(result["alpha_per_bar"], .001, delta=.0001)
        self.assertGreater(result["alpha_lower_bound"], 0)
        self.assertFalse(regression_test(-.001 + .4 * market + noise, market)["passed"])
        self.assertFalse(regression_test(.4 * market, market)["passed"])

    def test_regression_degenerate_and_invalid_inputs_fail(self):
        for y, x in ((np.ones(100), np.zeros(100)), (np.zeros(20), np.zeros(20)),
                     (np.ones(100), np.ones(99)), ([float('nan')]*100, np.arange(100))):
            self.assertFalse(regression_test(y, x)["passed"])

    def test_walk_forward_has_separated_dates_and_no_future_input(self):
        data = demo_data()
        h = variant_hypothesis(0).model_dump()
        seen = []
        def simulate(frame, rule, cfg, capital, start, end, **kwargs):
            self.assertEqual(len(frame), end)
            self.assertEqual(rule, h)
            seen.append((start, end))
            return successful(end-start)
        result = walk_forward_test(data, h, Settings(), 1540, simulate, lambda: None)
        self.assertTrue(result["passed"])
        self.assertEqual(len(seen), 6)
        for i in range(0, 6, 2):
            self.assertEqual(seen[i+1][0] - seen[i][1], h["max_holding"])
        self.assertEqual(seen[1][1], seen[3][0])
        self.assertEqual(seen[3][1], seen[5][0])
        self.assertLess(seen[0][1], seen[2][1])

    def test_walk_forward_rejects_sparse_or_unstable_windows(self):
        h = variant_hypothesis(0).model_dump()
        result = walk_forward_test(demo_data(), h, Settings(), 2190, lambda *a, **k: successful(), lambda: None)
        self.assertFalse(result["passed"])
        result = walk_forward_test(demo_data(), h, Settings(), 1540,
                                   lambda *a, **k: {**successful(), "trades": 0}, lambda: None)
        self.assertFalse(result["passed"])

    def test_parameter_stress_uses_bounded_variants_and_no_winner_selection(self):
        h = variant_hypothesis(0).model_dump()
        variants = list(parameter_variants(h))
        self.assertGreaterEqual(len(variants), 4)
        for key, _, variant in variants:
            Hypothesis.model_validate(variant)
            self.assertEqual(sum(h[k] != variant[k] for k in h), 1)
            self.assertNotEqual(key, "threshold")
        result = parameter_stress(demo_data(), h, Settings(), 1540,
                                  lambda *a, **k: {**successful(), "net_return": -.1}, lambda: None)
        self.assertFalse(result["passed"])
        self.assertEqual(result["passing_fraction"], 0)
        self.assertEqual(h, variant_hypothesis(0).model_dump())

    def test_monte_carlo_is_reproducible_and_requires_each_block_to_pass(self):
        cfg = Settings(monte_carlo_samples=200)
        positive = np.linspace(.0001, .0003, 120)
        a = monte_carlo_test(positive, cfg, lambda: None)
        self.assertEqual(a, monte_carlo_test(positive, cfg, lambda: None))
        self.assertTrue(a["passed"])
        self.assertEqual([row["block_bars"] for row in a["scenarios"]], [5, 10, 20])
        self.assertFalse(monte_carlo_test(-positive, cfg, lambda: None)["passed"])
        self.assertFalse(monte_carlo_test(np.zeros(120), cfg, lambda: None)["passed"])
        self.assertFalse(monte_carlo_test(np.r_[positive, -1], cfg, lambda: None)["passed"])
        json.dumps(a, allow_nan=False)

    def test_cancellation_interrupts_simulations(self):
        def stop():
            raise InterruptedError("stop")
        with self.assertRaises(InterruptedError):
            monte_carlo_test(np.linspace(.0001, .0003, 120), Settings(), stop)

    def test_concentration_rejects_one_off_profit(self):
        self.assertFalse(concentration_test(np.r_[.5, np.full(100, -.001)])["passed"])
        self.assertTrue(concentration_test(np.linspace(.0001, .0003, 120))["passed"])

    def test_signal_delay_does_not_wrap_negative_indices(self):
        data = pd.DataFrame({"open": [100., 100., 100., 100.], "close": [100., 100., 100., 100.]})
        h = variant_hypothesis(0).model_dump()
        with patch("trading_agent.signals", return_value=np.array([False, False, False, True])):
            self.assertEqual(backtest(data, h, Settings(), 10000, signal_delay=1)["trades"], 0)
        with self.assertRaises(ValueError):
            backtest(data, h, Settings(), 10000, signal_delay=-1)

    def test_a_failed_robustness_gate_skips_later_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent(demo_data(), Settings(), Path(tmp)/"run", demo=True)
            state = {**initial_state(), "hypothesis": variant_hypothesis(0).model_dump(), "history": [{}]}
            with patch("trading_agent.backtest", return_value=successful()), \
                 patch("trading_agent.parameter_stress", return_value={"passed": False, "status": "FAILED", "reason": "fragile"}), \
                 patch("trading_agent.monte_carlo_test") as mc:
                result = agent._stress_single(state)
            self.assertFalse(result["stress_metrics"]["passed"])
            mc.assert_not_called()
            self.assertEqual(result["stress_metrics"]["robustness_tests"]["monte_carlo"]["status"], "SKIPPED")

    def test_asset_switch_occurs_after_all_gates_for_each_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent({a: demo_data() for a in ("EURUSD", "BTCUSD")}, Settings(), Path(tmp)/"run", demo=True)
            state = {**initial_state(), "iteration_count": 1, "hypothesis": variant_hypothesis(0).model_dump()}
            calls = []
            def asset():
                return next(a for a, data in agent.datasets.items() if data is agent.data)
            def quant(s):
                calls.append((asset(), "quant"))
                return {"quant_metrics": {"passed": True, "out_of_sample": {"net_return": .1, "sharpe": 1}}, "charts": {}, "logs": []}
            def stress(s):
                calls.append((asset(), "stress"))
                return {"stress_metrics": {"passed": asset() == "BTCUSD"}, "charts": {}, "logs": []}
            with patch.object(agent, "_quant_single", side_effect=quant), patch.object(agent, "_stress_single", side_effect=stress):
                state.update(agent.quant_validator_node(state))
                state.update(agent.stress_test_node(state))
            self.assertEqual(calls, [("EURUSD", "quant"), ("EURUSD", "stress"), ("BTCUSD", "quant"), ("BTCUSD", "stress")])
            self.assertEqual(state["selected_asset"], "BTCUSD")
            self.assertTrue(state["stress_metrics"]["passed"])

    def test_all_assets_fail_then_research_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent({a: demo_data() for a in ("EURUSD", "BTCUSD")}, Settings(), Path(tmp)/"run", demo=True)
            state = {**initial_state(), "iteration_count": 1, "hypothesis": variant_hypothesis(0).model_dump()}
            with patch.object(agent, "_quant_single", return_value={"quant_metrics": {"passed": False, "out_of_sample": {"net_return": -.1}}, "charts": {}, "logs": []}), \
                 patch.object(agent, "_stress_single") as stress:
                state.update(agent.quant_validator_node(state))
            stress.assert_not_called()
            self.assertEqual(state["status"], "REJECTED")
            self.assertEqual(agent.should_continue_after_report(state), "researcher_node")
            self.assertEqual(len(state["asset_results"]), 2)

    def test_report_differentiates_failed_and_skipped(self):
        state = initial_state()
        state["quant_metrics"] = {"advanced_tests": {
            "regression": {"passed": False, "status": "FAILED", "reason": "negative alpha"},
            "walk_forward": {"passed": False, "status": "SKIPPED", "reason": "previous failure"}}}
        report = build_report(state, asdict(Settings()), True)
        checks = {c["label"]: c for c in report["checks"]}
        self.assertIs(checks["Regresión histórica alfa/beta · diagnóstico"]["passed"], False)
        self.assertIsNone(checks["Walk-forward cronológico"]["passed"])
        self.assertIn("negative alpha", render_html(report))

    def test_export_and_report_cannot_approve_incomplete_validation(self):
        state = {**initial_state(), "status": "APPROVED", "quant_metrics": {"passed": True}, "stress_metrics": {"passed": True}}
        self.assertEqual(build_report(state, asdict(Settings()), False)["outcome"], "REJECTED")
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent(demo_data(), Settings(), Path(tmp)/"run", demo=True)
            agent.demo = False
            with self.assertRaisesRegex(RuntimeError, "regresión"):
                agent.production_coder_node(state)


if __name__ == "__main__":
    unittest.main()
