import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from research_data import acquire_evidence, enrich_datasets, load_evidence_series, training_diagnostics
from strategy_rules import RuleProgram, evaluate_expression, program_requirements, program_signals
from trading_agent import (Hypothesis, Settings, TradingAgent, backtest, compact, demo_data,
                           hypothesis_signature, strategy_source, variant_hypothesis, initial_state, ResearchCandidate)
from validation import parameter_variants
from test_research import brief, evidence


def hypothesis(entry="pct(col('close'), horizon) > cutoff", exit="pct(col('close'), horizon) < -cutoff"):
    return Hypothesis(family="Difusión de información entre mercados", rationale="Una hipótesis libre con reglas explícitas y comprobables.",
                      allocation=.8, stop_loss=.1, take_profit=.3, max_holding=60,
                      program=RuleProgram(entry=entry, exit=exit, parameters=[
                          dict(name="horizon", value=12, lower=2, upper=90, integer=True),
                          dict(name="cutoff", value=.02, lower=.001, upper=.1, integer=False)]))


class ComposableRulesTests(unittest.TestCase):
    def test_no_family_catalogue_and_no_future_access(self):
        data = demo_data()
        h = hypothesis().model_dump()
        altered = data.copy()
        altered.loc[500:, "close"] *= 20
        for a, b in zip(program_signals(data, h["program"]), program_signals(altered, h["program"])):
            np.testing.assert_array_equal(a[:500], b[:500])
        self.assertEqual(program_requirements(h["program"]), ({"close"}, 12))

    def test_rejects_code_future_lags_and_undefined_parameters(self):
        for expression in ["__import__('os').system('echo unsafe')", "col('close').shift(-1)",
                           "lag(col('close'), -2) > 0", "sma(col('close'), 2.5) > 0",
                           "col('close')[0] > 0", "unknown > 0", "2 ** 9999999 > 0"]:
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                RuleProgram(entry=expression, exit="False")

    def test_missing_values_do_not_become_entries_through_not_or_inequality(self):
        data = pd.DataFrame({"value": [np.nan, 2., 0.]})
        for expression in ["not (col('value') > 1)", "col('value') != 1", "(1 / col('value')) > 0"]:
            self.assertFalse(evaluate_expression(expression, data, {})[0])
        self.assertFalse(evaluate_expression("(1 / col('value')) > 0", data, {})[2])
        with self.assertRaises(ValueError): evaluate_expression("col('value')", data, {})

    def test_entries_and_exits_are_independent_and_next_open(self):
        data = pd.DataFrame({"open": [100.] * 6, "close": [100., 100., 105., 110., 115., 120.],
                             "event": [1., 0., 0., 0., 0., 0.], "leave": [0., 0., 0., 1., 0., 0.]})
        h = hypothesis("col('event') > 0", "col('leave') > 0").model_dump()
        cfg = Settings(commission_rate=0, spread_bps=0, slippage_bps=0)
        result = backtest(data, h, cfg, 10000)
        self.assertEqual(result["trades"], 1)
        self.assertGreater(result["equity"][3], 10000)  # holding although event is now false
        self.assertEqual(result["equity"][5], 10000)  # exits on open after leave event

    def test_export_matches_program_and_has_no_runtime_project_imports(self):
        module = types.ModuleType("composable_export_test")
        sys.modules[module.__name__] = module
        try:
            h = hypothesis().model_dump()
            source = strategy_source(h, Settings(), {})
            self.assertNotIn("from strategy_rules", source)
            self.assertNotIn("from research_data", source)
            exec(compile(source, "export.py", "exec"), module.__dict__)
            data = demo_data()
            self.assertEqual(compact(backtest(data, h, Settings(), 10000)),
                             compact(module.backtest(data, h, module.Settings(), 10000)))
        finally:
            del sys.modules[module.__name__]

    def test_program_dedup_ignores_story_and_legacy_fields(self):
        h = hypothesis().model_dump()
        self.assertEqual(hypothesis_signature(h), hypothesis_signature({**h, "family": "Another narrative", "fast": 3}))
        changed = hypothesis("pct(col('close'), horizon) < cutoff").model_dump()
        self.assertNotEqual(hypothesis_signature(h), hypothesis_signature(changed))

    def test_custom_parameters_are_stressed(self):
        h = hypothesis().model_dump()
        variants = list(parameter_variants(h))
        self.assertTrue({"rule:horizon", "rule:cutoff"}.issubset({key for key, _, _ in variants}))
        self.assertEqual(h["program"]["parameters"][0]["value"], 12)
        for _, _, candidate in variants:
            RuleProgram.model_validate(candidate["program"])

    def test_no_three_family_variant_limit_for_composed_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent(demo_data(), Settings(), Path(tmp)/"run", demo=True)
            agent.demo = False
            for i in range(5):
                h = hypothesis(f"pct(col('close'), horizon) > {i / 100}")
                self.assertTrue(agent.register_hypothesis(h, i + 1))
                agent.memory.save(agent.run_id, i + 1, {}, "REJECTED")
            self.assertEqual(agent.trial_base, 4)
            self.assertFalse(agent.register_hypothesis(h, 6))

    def test_reject_contradictory_legacy_rules(self):
        h = variant_hypothesis(1).model_dump()
        with self.assertRaises(ValueError): Hypothesis.model_validate({**h, "regime": "uptrend"})

    def test_full_research_custom_program(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent(demo_data(), Settings(), Path(tmp)/"run", demo=True)
            agent.demo = False
            candidate = ResearchCandidate(brief=brief(), hypothesis=hypothesis())
            with patch.object(agent, "search_literature", return_value=evidence()), patch.object(agent, "call_model", return_value=candidate):
                state = initial_state()
                state.update(agent.researcher_node(state))
            self.assertEqual(state["status"], "RESEARCHING")
            state.update(agent.prototyper_node(state))
            result = agent.quant_validator_node(state)
            self.assertIn("out_of_sample", result["quant_metrics"])

    def test_target_assets_and_missing_features_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = demo_data()
            data["volume"] = 10.
            agent = TradingAgent({"A": data, "B": demo_data()}, Settings(), Path(tmp)/"run", demo=True)
            state = {**initial_state(), "iteration_count": 1,
                     "hypothesis": hypothesis("col('volume') > 1").model_dump(),
                     "research": {"brief": {"target_assets": ["A", "B"]}}}
            result = agent.quant_validator_node(state)
            self.assertIn("out_of_sample", result["asset_results"]["A"]["quant_metrics"])
            self.assertEqual(result["asset_results"]["B"]["quant_metrics"]["status"], "SKIPPED")


class ResearchDataTests(unittest.TestCase):
    def test_diagnostics_measure_next_open_and_exclude_incomplete_targets(self):
        data = demo_data().iloc[:100]
        result = training_diagnostics(data)
        daily = [row for row in result["calendar"] if row["horizon"] == 1]
        self.assertEqual(sum(row["observations"] for row in daily), 99)
        expected = (data.close.shift(-1) / data.open.shift(-1) - 1).dropna().mean()
        measured = sum(row["gross_return"] * row["observations"] for row in daily) / 99
        self.assertAlmostEqual(expected, measured)
        self.assertIn("no prueban", result["warning"])
        json.dumps(result, allow_nan=False)

    def test_volume_preserved_and_cross_market_is_delayed(self):
        data = demo_data()
        data["volume"] = np.arange(len(data))
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent({"A": data, "B": data}, Settings(), Path(tmp)/"run", demo=True)
            self.assertIn("volume", agent.data)
            self.assertTrue(np.isnan(agent.datasets["A"].market_b_close.iloc[0]))
            self.assertEqual(agent.datasets["A"].market_b_close.iloc[1], data.close.iloc[0])
            scope = agent.research_scope()
            self.assertEqual(agent.research_scope(), scope)

    def test_external_series_uses_availability_and_expiry(self):
        frame = demo_data().iloc[:8]
        times = frame.timestamp
        evidence = pd.DataFrame({"available_at": [times.iloc[2]], "value": [42.]})
        output = enrich_datasets({"A": frame}, {"macro": (evidence, 2)})["A"]
        self.assertTrue(output.external_macro.iloc[:3].isna().all())
        self.assertEqual(output.external_macro.iloc[3], 42)
        self.assertTrue(np.isnan(output.external_macro.iloc[5]))

    def test_acquisition_requires_traceable_url_and_publication_column(self):
        spec = dict(name="macro", source_url="https://example.org/data.csv", value_column="value",
                    availability_column="published", availability_description="Actual publication timestamp for each historically available release.", max_age_days=40)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "URL"):
                acquire_evidence([spec], set(), tmp, fetch=lambda _: b'published,value\n2020-01-01,2\n')
            series, metadata = acquire_evidence([spec], {spec["source_url"]}, tmp, fetch=lambda _: b'published,value\n2020-01-01,2\n')
            self.assertEqual(series["macro"][0].value.iloc[0], 2)
            self.assertEqual(len(metadata["macro"]["sha256"]), 64)
            with self.assertRaises(ValueError):
                acquire_evidence([spec], {spec["source_url"]}, tmp, fetch=lambda _: b'observation,value\n2020-01-01,2\n')
            with self.assertRaisesRegex(ValueError, "congelada"):
                acquire_evidence([spec], {spec["source_url"]}, tmp, fetch=lambda _: b'published,value\n2020-01-01,99\n')
            self.assertEqual(load_evidence_series(tmp)[0]["macro"][0].value.iloc[0], 2)

    def test_research_acquires_data_before_validating_program(self):
        from research_data import DataAcquisition
        spec = DataAcquisition(name="macro", source_url="https://example.org/paper", value_column="value",
                               availability_column="published", availability_description="Actual publication timestamp of the original release value.", max_age_days=5000)
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent(demo_data(), Settings(), Path(tmp)/"run", demo=True)
            agent.demo = False
            b = brief().model_copy(update={"required_fields": ["external_macro", "close"], "data_acquisition": [spec]})
            candidate = ResearchCandidate(brief=b, hypothesis=hypothesis("col('external_macro') > cutoff"))
            def acquisition(requests, urls, folder, checkpoint):
                return acquire_evidence(requests, urls, folder, checkpoint,
                                        fetch=lambda _: b'published,value\n2010-01-01,2\n')
            with patch.object(agent, "search_literature", return_value=evidence()), \
                 patch.object(agent, "call_model", return_value=candidate), \
                 patch("trading_agent.acquire_evidence", side_effect=acquisition):
                state = agent.researcher_node(initial_state())
            self.assertEqual(state["status"], "RESEARCHING")
            self.assertIn("external_macro", agent.data)
            self.assertIn("macro", state["research"]["acquired_data"])

    def test_cross_market_future_and_calendar_mismatch_are_causal(self):
        btc = demo_data().iloc[:30]
        fx = btc[btc.timestamp.dt.weekday < 5].reset_index(drop=True)
        first = enrich_datasets({"BTC": btc, "FX": fx})["BTC"]
        changed = fx.copy()
        cutoff = fx.timestamp.iloc[10]
        changed.loc[changed.timestamp >= cutoff, "close"] *= 10
        second = enrich_datasets({"BTC": btc, "FX": changed})["BTC"]
        np.testing.assert_array_equal(first.loc[first.timestamp <= cutoff, "market_fx_close"],
                                      second.loc[second.timestamp <= cutoff, "market_fx_close"])


if __name__ == "__main__":
    unittest.main()
