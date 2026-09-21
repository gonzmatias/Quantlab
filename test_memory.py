import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from research_memory import ResearchMemory, diagnose
from report_agent import assessment
from trading_agent import TradingAgent, Settings, demo_data, variant_hypothesis, initial_state
from validation import validation_complete


class MemoryTests(unittest.TestCase):
    def agent(self, root, name):
        agent = TradingAgent(demo_data(), Settings(), Path(root)/name, demo=True)
        agent.demo = False  # No remote API in unit tests; all use isolated temporary memory.
        return agent

    def test_duplicate_and_trials_persist_across_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self.agent(tmp, "one")
            h = variant_hypothesis(0)
            self.assertTrue(first.register_hypothesis(h, 1))
            first.memory.save(first.run_id, 1, {"reason": "known failure"}, "REJECTED")
            second = self.agent(tmp, "two")
            self.assertEqual(second.recent_research()[0]["reason"], "known failure")
            self.assertFalse(second.register_hypothesis(h, 1))
            self.assertTrue(second.register_hypothesis(variant_hypothesis(1), 2))
            self.assertEqual(second.trial_base, 1)

    def test_three_variant_budget_counts_training_rejections(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.agent(tmp, "one")
            h = variant_hypothesis(0)
            agent.register_hypothesis(h, 1)
            agent.memory.save(agent.run_id, 1, {}, "REJECTED")
            def simulation(frame, rule, *args, **kwargs):
                # Inferior candidates fail IS and must still consume the budget.
                self.assertEqual(len(frame), 1540)
                return {"sharpe": 1 if rule["slow"] == h.slow else .5, "trades": 40}
            with patch("trading_agent.backtest", side_effect=simulation):
                for attempt, slow in ((2, 30), (3, 40)):
                    with self.assertRaisesRegex(ValueError, "no mejora"):
                        agent.register_hypothesis(h.model_copy(update={"slow": slow}), attempt, "Justificación de horizonte basada sólo en entrenamiento.")
                    agent.memory.save(agent.run_id, attempt, {}, "REJECTED")
                with self.assertRaisesRegex(ValueError, "Presupuesto agotado"):
                    agent.register_hypothesis(h.model_copy(update={"slow": 50}), 4, "Justificación de horizonte basada sólo en entrenamiento.")

    def test_improved_training_variant_is_allowed_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.agent(tmp, "one")
            h = variant_hypothesis(0)
            agent.register_hypothesis(h, 1)
            agent.memory.save(agent.run_id, 1, {}, "REJECTED")
            with patch("trading_agent.backtest", side_effect=[{"sharpe": 2, "trades": 40}, {"sharpe": 1, "trades": 40}]):
                self.assertTrue(agent.register_hypothesis(h.model_copy(update={"slow": 30}), 2, "Justificación de horizonte basada sólo en entrenamiento."))
            self.assertTrue(agent.variant_screen["passed"])
            self.assertEqual(agent.trial_base, 1)

    def test_approved_structure_cannot_be_reformulated(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.agent(tmp, "one")
            h = variant_hypothesis(0)
            agent.register_hypothesis(h, 1)
            agent.memory.save(agent.run_id, 1, {}, "APPROVED")
            with self.assertRaisesRegex(ValueError, "aprobada"):
                agent.register_hypothesis(h.model_copy(update={"slow": 30}), 2, "Justificación de horizonte basada sólo en entrenamiento.")

    def test_demo_and_real_databases_do_not_mix(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ResearchMemory(Path(tmp)/"demo.sqlite3")
            b = ResearchMemory(Path(tmp)/"real.sqlite3")
            a.save("demo", 1, {"fake": True})
            self.assertEqual(b.recent(), [])

    def test_diagnosis_distinguishes_capital_costs_and_evidence(self):
        row = {"quant_metrics": {"rejection_reasons": ["Operaciones insuficientes"]},
               "stress_metrics": {"rejection_reasons": ["Costos x2: retorno inválido"],
                                  "scenarios": {"1": {"trades": 0, "skipped_orders": 50}}}}
        kinds = {d["category"] for d in diagnose(row)}
        self.assertTrue({"execution_limits", "costs", "insufficient_evidence"}.issubset(kinds))

    def test_diagnostic_failure_does_not_override_mandatory_results(self):
        q = {"passed": True, "advanced_tests": {"regression": {"passed": False}, "walk_forward": {"passed": True}},
             "out_of_sample": {"net_return": .1, "trades": 40}}
        s = {"passed": True, "robustness_tests": {k: {"passed": True} for k in ("parameters", "monte_carlo", "signal_delay")}}
        s["robustness_tests"]["concentration"] = {"passed": False}
        self.assertTrue(validation_complete(q, s))
        s["robustness_tests"]["monte_carlo"]["passed"] = False
        self.assertFalse(validation_complete(q, s))
        status = assessment(q, s)
        self.assertEqual(status["historical_profitability"], "POSITIVA EN OOS")
        self.assertEqual(status["robustness"], "NO APROBADA")
        self.assertEqual(status["independent_validation"], "PENDIENTE")

    def test_failed_regression_does_not_skip_walk_forward(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.agent(tmp, "one")
            def simulation(data, h, cfg, capital, start=0, end=None, **kwargs):
                n = (len(data) if end is None else end) - start
                return {"sharpe": 1., "net_return": .1, "max_drawdown": .01, "trades": 40,
                        "profit_factor": 2., "no_losing_trades": False, "bankrupt": False,
                        "returns": np.full(n, .001), "equity": np.linspace(capital, capital*1.1, n+1)}
            state = {**initial_state(), "hypothesis": variant_hypothesis(0).model_dump(), "iteration_count": 1}
            with patch("trading_agent.backtest", side_effect=simulation), \
                 patch("trading_agent.deflated_sharpe", return_value={"dsr": .99, "dsr_z": 5., "sequential_passed": True}), \
                 patch("trading_agent.regression_test", return_value={"passed": False}), \
                 patch("trading_agent.walk_forward_test", return_value={"passed": True}) as wf:
                result = agent._quant_single(state)
            wf.assert_called_once()
            self.assertTrue(result["quant_metrics"]["passed"])


if __name__ == "__main__":
    unittest.main()
