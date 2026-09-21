import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from experiment_protocol import HoldoutLedger, partition, benchmark_test, digest
from forward_validate import evaluate_candidate
from strategy_rules import validate_tunable_program
from trading_agent import TradingAgent, Settings, demo_data, initial_state, variant_hypothesis, validate_data
from validation import parameter_stress, validation_complete


def gates():
    return ({"passed": True, "benchmark": {"passed": True}, "advanced_tests": {"walk_forward": {"passed": True}}},
            {"passed": True, "robustness_tests": {k: {"passed": True} for k in ("parameters", "monte_carlo", "signal_delay")}})


class ProtocolTests(unittest.TestCase):
    def real_agent(self, root, name="run", data=None, **settings):
        return TradingAgent(demo_data() if data is None else data, Settings(**settings), Path(root)/name,
                            model="offline-fixture", api_key="offline-fixture", demo=False)

    def test_reserved_prices_never_reach_discovery_or_research_context(self):
        original = validate_data(demo_data())
        _, _, cutoff = partition({"CUSTOM": original})
        changed = original.copy()
        changed.loc[changed.timestamp >= cutoff, ["open", "high", "low", "close"]] *= 50
        with tempfile.TemporaryDirectory() as tmp:
            a = self.real_agent(tmp, "a", original)
            b = self.real_agent(tmp, "b", changed)
            pd.testing.assert_frame_equal(a.data, b.data)
            self.assertEqual(a.research_context(), b.research_context())
            self.assertLess(a.data.timestamp.max(), a.holdout_start)
            self.assertLess(pd.Timestamp(a.research_context()["CUSTOM"]["end"]), a.split_date)

    def test_global_ledger_blocks_overlap_across_restart_and_other_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"ledger.sqlite3"
            self.assertTrue(HoldoutLedger(path).reserve("2024-01-01", "2024-12-31", "one", "a"))
            self.assertFalse(HoldoutLedger(path).reserve("2024-06-01", "2025-02-01", "other-settings", "b"))
            self.assertTrue(HoldoutLedger(path).reserve("2025-01-01", "2025-12-31", "three", "c"))

    def test_real_search_requires_budget_and_positive_capital(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "presupuesto"):
                self.real_agent(tmp, max_iterations=0)
        with self.assertRaises(ValueError):
            Settings(capital=0)

    def test_final_failure_is_terminal_and_excluded_from_research_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.real_agent(tmp)
            q, s = gates()
            state = {**initial_state(), "quant_metrics": q, "stress_metrics": s,
                     "hypothesis": variant_hypothesis(0).model_dump(), "iteration_count": 1}
            with patch("trading_agent.evaluate_frozen", return_value={"passed": False, "status": "FAILED", "reason": "secret final metric"}):
                state.update(agent.final_validator_node(state))
            self.assertEqual(agent.after_final(state), "reporter_node")
            self.assertEqual(agent.should_continue_after_report(state), "__end__")
            self.assertNotIn("secret final metric", json.dumps(agent.recent_research()))
            frozen = json.loads((agent.output/"candidate.json").read_text())
            self.assertEqual(frozen["sha256"], digest({k: v for k, v in frozen.items() if k != "sha256"}))
            with self.assertRaisesRegex(ValueError, "consumida"):
                self.real_agent(tmp, "restart", capital=20000)

    def test_crash_consumes_holdout_before_evaluation(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.real_agent(tmp)
            q, s = gates()
            state = {**initial_state(), "quant_metrics": q, "stress_metrics": s, "hypothesis": variant_hypothesis(0).model_dump()}
            with patch("trading_agent.evaluate_frozen", side_effect=RuntimeError("crash")):
                with self.assertRaises(RuntimeError):
                    agent.final_validator_node(state)
            with self.assertRaisesRegex(ValueError, "consumida"):
                self.real_agent(tmp, "restart")

    def test_numeric_literals_and_cosmetic_parameters_cannot_pass_robustness(self):
        h = variant_hypothesis(0).model_dump()
        h["program"] = {"entry": "pct(col('close'), 20) > .05", "exit": "False", "parameters": []}
        simulator = unittest.mock.Mock()
        result = parameter_stress(demo_data(), h, Settings(), 1540, simulator, lambda: None)
        self.assertFalse(result["passed"])
        simulator.assert_not_called()
        for params in ([dict(name="x", value=20, lower=20, upper=20, integer=True)],
                       [dict(name="x", value=20, lower=19, upper=21, integer=True)]):
            with self.assertRaisesRegex(ValueError, "rango"):
                validate_tunable_program({"entry": "col('close') > x", "exit": "False", "parameters": params})
        validate_tunable_program({"entry": "col('close') > x", "exit": "False",
                                  "parameters": [dict(name="x", value=20, lower=10, upper=30, integer=True)]})

    def test_buy_and_hold_disguised_as_strategy_has_no_advantage(self):
        from trading_agent import backtest
        data = validate_data(demo_data())
        h = {**variant_hypothesis(0).model_dump(), "program": {"entry": "True", "exit": "False", "parameters": []},
             "max_holding": 10000, "stop_loss": .999999, "take_profit": 1e50}
        result = benchmark_test(data, h, Settings(), 1540, backtest)
        self.assertFalse(result["passed"])
        self.assertEqual(result["strategy"]["net_return"], result["buy_and_hold"]["net_return"])
        q, s = gates()
        q.pop("benchmark")
        self.assertFalse(validation_complete(q, s))

    def test_capital_is_used_in_each_stress_scenario(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent(demo_data(), Settings(capital=4321), Path(tmp)/"run", demo=True)
            capitals = []
            def simulate(data, h, cfg, capital, *args, **kwargs):
                capitals.append(capital)
                return {"net_return": 0, "max_drawdown": 0, "bankrupt": False, "trades": 0}
            state = {**initial_state(), "hypothesis": variant_hypothesis(0).model_dump(), "history": [{}]}
            with patch("trading_agent.backtest", side_effect=simulate):
                result = agent._stress_single(state)
            self.assertEqual(capitals, [4321, 4321, 4321])
            self.assertEqual(result["stress_metrics"]["initial_capital"], 4321)

    def test_forward_waits_for_unseen_bars_and_refuses_modified_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.real_agent(tmp)
            q, s = gates()
            state = {**initial_state(), "quant_metrics": q, "stress_metrics": s, "hypothesis": variant_hypothesis(0).model_dump()}
            with patch("trading_agent.evaluate_frozen", return_value={"passed": False, "reason": "fixture"}):
                agent.final_validator_node(state)
            path = agent.output/"candidate.json"
            result = evaluate_candidate(path, {"CUSTOM": validate_data(demo_data())})
            self.assertEqual(result["status"], "PENDING")
            payload = json.loads(path.read_text())
            payload["settings"]["capital"] *= 2
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "modificado"):
                evaluate_candidate(path, {})

    def test_forward_evaluates_only_first_120_new_bars_and_never_retries_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.real_agent(tmp)
            q, s = gates()
            state = {**initial_state(), "quant_metrics": q, "stress_metrics": s, "hypothesis": variant_hypothesis(0).model_dump()}
            with patch("trading_agent.evaluate_frozen", return_value={"passed": False, "reason": "fixture"}):
                agent.final_validator_node(state)
            path = agent.output/"candidate.json"
            payload = json.loads(path.read_text())
            old = validate_data(demo_data())
            new = old.iloc[:180].copy()
            first = max(pd.Timestamp(payload["observed_through"]), pd.Timestamp(payload["frozen_at"]).normalize()) + pd.Timedelta(days=1)
            new["timestamp"] = pd.date_range(first, periods=180, freq="B")
            data = {"CUSTOM": pd.concat([old, new], ignore_index=True)}
            seen = []
            def evaluate(frame, h, cfg, start):
                seen.append((len(frame)-start, frame.timestamp.iloc[start]))
                return {"passed": False, "status": "FAILED"}
            with patch("forward_validate.evaluate_frozen", side_effect=evaluate) as evaluator:
                first_result = evaluate_candidate(path, data)
                second_result = evaluate_candidate(path, data)
            self.assertEqual(first_result, second_result)
            self.assertFalse(first_result["passed"])
            evaluator.assert_called_once()
            self.assertEqual(seen[0][0], 120)
            self.assertGreater(seen[0][1], pd.Timestamp(payload["frozen_at"]))

    def test_final_graph_failure_does_not_request_another_hypothesis(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.real_agent(tmp)
            q, s = gates()
            research = {"hypothesis": variant_hypothesis(0).model_dump(), "iteration_count": 1, "status": "RESEARCHING"}
            with patch.object(agent, "researcher_node", return_value=research) as researcher, \
                 patch.object(agent, "quant_validator_node", return_value={"quant_metrics": q}), \
                 patch.object(agent, "stress_test_node", return_value={"stress_metrics": s}), \
                 patch("trading_agent.evaluate_frozen", return_value={"passed": False, "status": "FAILED", "reason": "final rejected"}), \
                 patch.object(agent, "production_coder_node") as production:
                result = agent.run()
            researcher.assert_called_once()
            production.assert_not_called()
            self.assertEqual(result["status"], "FINAL_REJECTED")
            self.assertIn("final rejected", result["latest_report"]["rejection_reasons"])


if __name__ == "__main__":
    unittest.main()
