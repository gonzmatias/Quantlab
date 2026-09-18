import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
from langgraph.graph import END

from trading_agent import (Settings, TradingAgent, backtest, compact, deflated_sharpe,
                           demo_data, initial_state, signals, strategy_source, validate_data)


H = dict(family="trend", rationale="Persistence in prices due to gradual information diffusion",
         fast=2, slow=10, threshold=1., allocation=.9, stop_loss=.03,
         take_profit=.06, max_holding=20)


class AgentTests(unittest.TestCase):
    def test_no_lookahead(self):
        data = demo_data()
        altered = data.copy()
        altered.loc[500:, ["open", "high", "low", "close"]] *= 10
        np.testing.assert_array_equal(signals(data, H)[:500], signals(altered, H)[:500])
        a = backtest(data, H, Settings(), 100, 0, 500)
        b = backtest(altered, H, Settings(), 100, 0, 500)
        np.testing.assert_array_equal(a["equity"], b["equity"])

    def test_round_trip_fees(self):
        data = pd.DataFrame({"open": [100.] * 4, "close": [100.] * 4})
        cfg = Settings(commission_rate=.01, fixed_commission=1, spread_bps=0,
                       slippage_bps=0, min_notional=1, quantity_step=1)
        with patch("trading_agent.signals", return_value=np.array([True, False, False, False])):
            r = backtest(data, H, cfg, 1000)
        # Eight shares: buy 800 + 8 + 1; sell 800 - 8 - 1.
        self.assertEqual(r["trades"], 1)
        self.assertAlmostEqual(r["final_equity"], 982)

    def test_minimum_position_blocks_orders(self):
        cfg = Settings(min_notional=1000)
        r = backtest(demo_data(), H, cfg, 100)
        self.assertEqual(r["trades"], 0)
        self.assertEqual(r["final_equity"], 100)
        self.assertGreater(r["skipped_orders"], 0)

    def test_dsr_and_search_penalty(self):
        r = np.random.default_rng(4).normal(.001, .01, 1000)
        one = deflated_sharpe(r, Settings(dsr_trial_budget=1))
        ten = deflated_sharpe(r, Settings(dsr_trial_budget=10))
        self.assertLess(ten["dsr"], one["dsr"])
        self.assertTrue(0 <= ten["dsr"] <= 1)
        self.assertEqual(deflated_sharpe(np.zeros(100), Settings())["dsr"], 0)
        with self.assertRaises(ValueError):
            Settings(dsr_probability_min=1.2)

    def test_ten_attempts_and_no_production(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent(demo_data(), Settings(max_iterations=10, min_trades=100000), Path(tmp)/"run", demo=True)
            result = agent.run()
            self.assertEqual(result["iteration_count"], 10)
            self.assertEqual(result["status"], "REJECTED")
            self.assertEqual(result["production_code"], "")
            self.assertFalse((agent.output / "production_strategy.py").exists())
            self.assertEqual(json.loads((agent.output/"state.json").read_text("utf-8"))["iteration_count"], 10)

    def test_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent(demo_data(), Settings(max_iterations=10), Path(tmp)/"run", demo=True)
            state = initial_state()
            state.update(iteration_count=9, status="REJECTED")
            self.assertEqual(agent.should_continue_after_quant(state), "reporter_node")
            self.assertEqual(agent.should_continue_after_stress(state), "reporter_node")
            self.assertEqual(agent.should_continue_after_report(state), "researcher_node")
            state["iteration_count"] = 10
            self.assertEqual(agent.should_continue_after_quant(state), "reporter_node")
            self.assertEqual(agent.should_continue_after_stress(state), "reporter_node")
            self.assertEqual(agent.should_continue_after_report(state), END)
            state.update(status="RESEARCHING", quant_metrics={"passed": True}, stress_metrics={"passed": True})
            self.assertEqual(agent.should_continue_after_quant(state), "stress_test_node")
            self.assertEqual(agent.should_continue_after_stress(state), "production_coder_node")

    def test_export_matches_engine(self):
        source = strategy_source(H, Settings(), {"summary": "Quotes ' and newlines\n are data"})
        # Execute only our deterministic template in a registered module, never LLM code.
        import sys
        import types
        module = types.ModuleType("exported_test")
        sys.modules[module.__name__] = module
        try:
            exec(compile(source, "exported_test.py", "exec"), module.__dict__)
            data = demo_data()
            original = backtest(data, H, Settings(), 100)
            exported = module.backtest(data, H, module.Settings(), 100)
            self.assertEqual(compact(original), compact(exported))
            np.testing.assert_array_equal(original["equity"], exported["equity"])
        finally:
            del sys.modules[module.__name__]

    def test_invalid_data(self):
        data = demo_data()
        data.loc[1, "timestamp"] = data.loc[0, "timestamp"]
        with self.assertRaises(ValueError):
            validate_data(data)

    def test_stress_rejects_unaffordable_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent(demo_data(), Settings(min_notional=1000), Path(tmp)/"run", demo=True)
            state = initial_state()
            state.update(hypothesis=H, history=[{"hypothesis": H}])
            result = agent.stress_test_node(state)
            self.assertEqual(result["status"], "REJECTED")
            self.assertFalse(result["stress_metrics"]["passed"])
            for scenario in result["stress_metrics"]["scenarios"].values():
                self.assertEqual(scenario["final_equity"], 100)
                self.assertEqual(scenario["trades"], 0)

    def test_stress_drawdown_and_bankruptcy(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent(demo_data(), Settings(), Path(tmp)/"run", demo=True)
            state = initial_state()
            state.update(hypothesis=H, history=[{"hypothesis": H}])
            baseline = dict(bankrupt=False, max_drawdown=.1, net_return=.1, trades=40)
            for change in ({"max_drawdown": .26}, {"bankrupt": True}, {"net_return": 0}, {"trades": 1}):
                with patch("trading_agent.backtest", return_value={**baseline, **change}):
                    self.assertFalse(agent.stress_test_node(state)["stress_metrics"]["passed"])
            with patch("trading_agent.backtest", return_value=baseline):
                self.assertTrue(agent.stress_test_node(state)["stress_metrics"]["passed"])

    def test_production_requires_filters_and_real_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent(demo_data(), Settings(), Path(tmp)/"run", demo=True)
            state = initial_state()
            with self.assertRaises(RuntimeError):
                agent.production_coder_node(state)
            state.update(quant_metrics={"passed": True}, stress_metrics={"passed": True})
            self.assertEqual(agent.production_coder_node(state)["status"], "REJECTED")
            self.assertFalse((agent.output / "production_strategy.py").exists())


if __name__ == "__main__":
    unittest.main()
