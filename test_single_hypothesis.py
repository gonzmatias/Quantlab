import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import JobManager
from langgraph.graph import END
from test_research import brief
from test_strategy_rules import hypothesis
from trading_agent import (Settings, TradingAgent, ResearchCandidate,
                           UserHypothesisTranslation, demo_data, initial_state)


TEXT = "Probar una hipótesis propia con reglas explícitas, sin buscar otras estrategias."


class SingleHypothesisTests(unittest.TestCase):
    def agent(self, directory):
        with patch("langchain_openai.ChatOpenAI"):
            return TradingAgent(demo_data(), Settings(max_iterations=30), Path(directory)/"run",
                                model="test", api_key="test", natural_hypothesis=TEXT)

    def translation(self, **changes):
        dossier = brief().model_copy(update={"sources": [], "origin": "conjecture", **changes})
        return UserHypothesisTranslation(missing_details=[], candidate=ResearchCandidate(
            brief=dossier, hypothesis=hypothesis()))

    def test_translation_has_no_market_performance_or_search_and_preserves_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.agent(tmp)
            translated = self.translation()
            with patch.object(agent, "call_model", return_value=translated) as llm, \
                 patch.object(agent, "search_literature") as search, \
                 patch.object(agent, "recent_research") as memory:
                state = {**initial_state(), **agent.researcher_node(initial_state())}
            search.assert_not_called()
            memory.assert_not_called()
            self.assertEqual(llm.call_count, 1)
            prompt = llm.call_args.args[1]
            self.assertIn(TEXT, prompt)
            self.assertNotIn('"training_context"', prompt)
            self.assertNotIn('"net_return"', prompt)
            self.assertNotIn('"diagnostics"', prompt)
            self.assertNotIn('"mean"', prompt)
            self.assertNotIn('"std"', prompt)
            self.assertEqual(state["hypothesis"], translated.candidate.hypothesis.model_dump())
            self.assertEqual(state["research"]["original_hypothesis"], TEXT)
            self.assertEqual(agent.cfg.max_iterations, 1)
            self.assertEqual(agent.should_continue_after_report(state), END)
            self.assertEqual(state["status"], "RESEARCHING")
            agent.memory.export_audit(agent.output / "audit_test.json")
            audit = json.loads((agent.output / "audit_test.json").read_text("utf-8"))
            self.assertIn("TRIAL_RESERVED", json.dumps(audit))

    def test_missing_details_ends_once_with_report_without_backtest_or_holdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.agent(tmp)
            translation = UserHypothesisTranslation(missing_details=["Indica la salida y el activo"], candidate=None)
            with patch.object(agent, "call_model", return_value=translation) as llm, \
                 patch("trading_agent.backtest") as backtest, \
                 patch.object(agent, "final_validator_node") as final:
                state = agent.run()
            self.assertEqual(llm.call_count, 1)
            backtest.assert_not_called()
            final.assert_not_called()
            self.assertEqual(state["iteration_count"], 1)
            report = json.loads((agent.output/"reports/attempt_01.json").read_text("utf-8"))
            self.assertEqual(report["research"]["missing_details"], translation.missing_details)
            self.assertIn(TEXT, (agent.output/"reports/attempt_01.html").read_text("utf-8"))
            self.assertIsNone(agent.holdout_ledger.latest_end())

    def test_unsupported_execution_goes_to_backlog(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.agent(tmp)
            with patch.object(agent, "call_model", return_value=self.translation(requires_ticks=True)):
                state = agent.researcher_node(initial_state())
            self.assertEqual(state["research"]["outcome_category"], "NEEDS_CAPABILITY")
            self.assertEqual(len(list((Path(tmp)/"hypotheses_backlog").glob("*.json"))), 1)

    def test_invalid_input_does_not_start_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = JobManager(Path(tmp))
            for payload in ({"run_mode": "other"},
                            {"run_mode": "single", "hypothesis": ""},
                            {"run_mode": "single", "hypothesis": TEXT, "mode": "demo"}):
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    manager.start(payload)
            self.assertFalse(manager.busy)

    def test_dashboard_passes_single_mode_and_forces_one_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = JobManager(Path(tmp))
            with patch("dashboard.threading.Thread") as thread:
                manager.start({"run_mode": "single", "hypothesis": TEXT,
                               "api_key": "test", "model": "test", "settings": {"max_iterations": 30}})
            self.assertEqual(manager.settings.max_iterations, 1)
            self.assertEqual(thread.call_args.kwargs["args"][-1], TEXT)


if __name__ == "__main__":
    unittest.main()
