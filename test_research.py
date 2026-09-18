import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from research import ResearchBrief, extract_web_evidence, validate_brief
from report_agent import build_report, render_html
from trading_agent import (ResearchCandidate, Settings, TradingAgent, demo_data,
                           initial_state, signals, hypothesis_signature, variant_hypothesis,
                           strategy_source, backtest, compact)
from dataclasses import asdict


URL = "https://example.org/paper"


def brief():
    return ResearchBrief(
        sources=[dict(url=URL, title="Test paper", finding="Published hypothesis about conditional trend persistence.",
                      limitations="The published sample is not evidence for our instruments.", source_kind="paper")],
        mechanism="Information may spread gradually after a period of low volatility.",
        prediction="Filtered entries should outperform unfiltered entries after costs.",
        falsification="Reject if the effect disappears after costs or in later windows.",
        adaptation="Use daily OHLC and prior volatility; no volume or intraday execution is available.",
        parameter_reasoning="The selected horizon represents several weeks of adjustment.",
        target_assets=["CUSTOM"], required_fields=["close", "high", "low"], timeframe="1d",
        requires_shorting=False, requires_leverage=False, compatible=True,
        compatibility_reason="All required price fields and daily execution are supported.")


def evidence():
    return {"sources": [{"url": URL, "title": "Test paper"}], "summary": "Test research"}


class ResearchTests(unittest.TestCase):
    def agent(self, tmp):
        agent = TradingAgent(demo_data(), Settings(max_iterations=1), Path(tmp)/"run", demo=True)
        agent.demo = False
        return agent

    def test_requires_actual_search_metadata(self):
        response = {"status": "completed", "output": [
            {"type": "web_search_call", "status": "completed", "action": {"type": "search", "sources": [{"url": URL}]}},
            {"type": "message", "content": [{"type": "output_text", "text": "Some finding", "annotations": []}]}]}
        self.assertEqual(extract_web_evidence(response)["sources"][0]["url"], URL)
        response["output"].pop(0)
        with self.assertRaises(RuntimeError): extract_web_evidence(response)

    def test_untraceable_or_incompatible_proposal_rejected(self):
        for change in ({"sources": [{**brief().sources[0].model_dump(), "url": "https://invented.org"}]},
                       {"required_fields": ["volume"]}, {"timeframe": "1m"}, {"requires_shorting": True},
                       {"requires_leverage": True}, {"target_assets": ["UNKNOWN"]}, {"compatible": False}):
            with self.subTest(change=change):
                candidate = ResearchBrief.model_validate({**brief().model_dump(), **change})
                with self.assertRaises(ValueError): validate_brief(candidate, evidence(), {"CUSTOM"})

    def test_real_duplicate_rejected_without_parameter_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.agent(tmp)
            candidate = ResearchCandidate(brief=brief(), hypothesis=variant_hypothesis(0))
            with patch.object(agent, "search_literature", return_value=evidence()), patch.object(agent, "call_model", return_value=candidate), patch("trading_agent.variant_hypothesis") as fallback:
                state = initial_state()
                state.update(agent.researcher_node(state))
                self.assertEqual(state["status"], "RESEARCHING")
                second = agent.researcher_node(state)
                self.assertEqual(second["status"], "REJECTED")
                fallback.assert_not_called()
            self.assertTrue((agent.output / "research/attempt_02.json").exists())
            self.assertIn("repetidas", second["research"]["rejection_reason"])
            self.assertEqual(len(agent.recent_research()), 2)

    def test_parameter_only_changes_are_not_new_research(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.agent(tmp)
            h = variant_hypothesis(0)
            self.assertTrue(agent.register_hypothesis(h, 1))
            with self.assertRaisesRegex(ValueError, "estructurales repetidas"):
                agent.register_hypothesis(h.model_copy(update={"slow": 80}), 2)
            self.assertTrue(agent.register_hypothesis(h.model_copy(update={"regime": "low_volatility"}), 3))

    def test_web_request_uses_tool_and_returns_provenance(self):
        payload = {"id": "test", "status": "completed", "output": [
            {"type": "web_search_call", "status": "completed", "action": {"sources": [{"url": URL}]}},
            {"type": "message", "content": [{"type": "output_text", "text": "Research summary"}]}]}
        from unittest.mock import AsyncMock, MagicMock
        response = MagicMock()
        response.model_dump.return_value = payload
        client = MagicMock()
        client.responses.create = AsyncMock(return_value=response)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.agent(tmp)
            agent.model = "configured-model"
            with patch("openai.AsyncOpenAI", return_value=client):
                result = agent.search_literature(agent.research_context(), [])
            request = client.responses.create.call_args.kwargs
            self.assertEqual(request["model"], "configured-model")
            self.assertEqual(request["tools"], [{"type": "web_search"}])
            self.assertEqual(request["tool_choice"], "required")
            self.assertFalse(request["store"])
            self.assertEqual(result["sources"][0]["url"], URL)
            self.assertIn("retrieved_at", result)

    def test_memory_keeps_training_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.agent(tmp)
            state = initial_state()
            state.update(iteration_count=1, status="REJECTED", research={"mode": "web", "brief": brief().model_dump()},
                         asset_results={"CUSTOM": {"quant_metrics": {"in_sample": {"net_return": .1},
                             "out_of_sample": {"secret_oos": 999}}, "stress_metrics": {}}})
            agent.reporter_node(state)
            import json
            memory = json.dumps(agent.recent_research())
            self.assertIn('"net_return": 0.1', memory)
            self.assertNotIn("secret_oos", memory)

    def test_training_context_ignores_future(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.agent(tmp)
            before = agent.research_context()
            agent.data.loc[agent.split_index():, ["open", "high", "low", "close"]] *= 100
            self.assertEqual(before, agent.research_context())

    def test_research_prompt_has_no_oos_feedback_and_saves_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.agent(tmp)
            state = initial_state()
            state["logs"] = ["SECRET_OOS_RESULT"]
            candidate = ResearchCandidate(brief=brief(), hypothesis=variant_hypothesis(0))
            with patch.object(agent, "search_literature", return_value=evidence()), patch.object(agent, "call_model", return_value=candidate) as model:
                state.update(agent.researcher_node(state))
            self.assertNotIn("SECRET_OOS_RESULT", model.call_args.args[1])
            report = build_report(state, asdict(agent.cfg), False)
            self.assertIn('href="https://example.org/paper"', render_html(report))
            self.assertEqual(report["research"]["brief"]["prediction"], brief().prediction)

    def test_incompatible_candidate_skips_backtest(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.agent(tmp)
            invalid = brief().model_copy(update={"required_fields": ["order_book"]})
            candidate = ResearchCandidate(brief=invalid, hypothesis=variant_hypothesis(0))
            with patch.object(agent, "search_literature", return_value=evidence()), patch.object(agent, "call_model", return_value=candidate):
                state = initial_state()
                state.update(agent.researcher_node(state))
            with patch("trading_agent.backtest") as simulate:
                self.assertEqual(agent.prototyper_node(state), {})
                self.assertEqual(agent.quant_validator_node(state), {})
                simulate.assert_not_called()

    def test_filters_are_causal_and_affect_signature(self):
        data = demo_data()
        altered = data.copy()
        altered.loc[500:, ["open", "high", "low", "close"]] *= 10
        h = variant_hypothesis(2).model_dump()
        for regime in ("none", "uptrend", "low_volatility"):
            filtered = {**h, "regime": regime, "confirmation": "strong_close"}
            np.testing.assert_array_equal(signals(data, filtered)[:500], signals(altered, filtered)[:500])
        filtered = {**h, "regime": "low_volatility"}
        self.assertNotEqual(hypothesis_signature(filtered), hypothesis_signature({**filtered, "fast": 3}))
        self.assertEqual(hypothesis_signature(h), hypothesis_signature({**h, "fast": 3}))

    def test_export_matches_filtered_engine(self):
        import sys
        import types
        h = {**variant_hypothesis(0).model_dump(), "regime": "low_volatility", "confirmation": "strong_close"}
        module = types.ModuleType("research_export_test")
        sys.modules[module.__name__] = module
        try:
            exec(compile(strategy_source(h, Settings(), {}), "export.py", "exec"), module.__dict__)
            data = demo_data()
            self.assertEqual(compact(backtest(data, h, Settings(), 10000)), compact(module.backtest(data, h, module.Settings(), 10000)))
        finally:
            del sys.modules[module.__name__]


if __name__ == "__main__":
    unittest.main()
