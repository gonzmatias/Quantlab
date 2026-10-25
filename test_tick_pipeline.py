import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from gherkin_pipeline import Translation, evaluate, load_bundle, translate
from gherkin_spec import parse_markdown, run_scenarios
from strategy_rules import RuleProgram
from tick_data import load_tick_manifest

TEXT = (Path(__file__).parent / "examples/strategy-ticks-gherkin.md").read_text("utf-8")
PROGRAM = {"entry":"col('bid') == 1.0999","exit":"col('bid') < 1.0991","parameters":[]}


def translator(document,model,key):
    return Translation(missing_details=[],unsupported=[],program=RuleProgram(**PROGRAM),
                       entry_source=document["rules"][0]["text"],exit_source=document["rules"][1]["text"])


class TickPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        p = patch("gherkin_pipeline.LEDGER_ROOT",self.root/"ledger")
        p.start()
        self.addCleanup(p.stop)

    def manifest(self, document):
        c = document["contract"]
        times = pd.date_range(c["evaluation"]["start"],c["evaluation"]["end"],freq="12h",inclusive="left")
        pd.DataFrame({"timestamp":times,"sequence":range(len(times)),"bid":1.09,"ask":1.0901}).to_csv(self.root/"ticks.csv",index=False)
        spec = {k:c[k] for k in ("asset","source","contract","instrument_type","quote_currency")}
        spec.update(timeframe="tick",file="ticks.csv",settings=c["costs"],
                    coverage_start=c["evaluation"]["start"],coverage_end=c["evaluation"]["end"])
        path = self.root/"manifest.json"
        path.write_text(json.dumps({"datasets":[spec]}),encoding="utf-8")
        return path

    def test_template_conformance_and_roundtrip(self):
        doc = parse_markdown(TEXT)
        scenarios = run_scenarios(doc,PROGRAM)
        self.assertEqual(len(scenarios),3)
        self.assertEqual(scenarios[1]["actual"]["trades"][0]["exit_reason"],"stop")
        bundle = translate(TEXT,translator=translator)
        self.assertEqual(load_bundle(bundle),doc)

    def test_pipeline_without_ai_uses_time_returns_and_preserves_reserve(self):
        bundle = translate(TEXT,translator=translator)
        manifest = self.manifest(bundle["document"])
        with patch("gherkin_pipeline.ask_model",side_effect=AssertionError("No AI")), patch.dict("os.environ",{},clear=True), \
             patch("socket.socket.connect",side_effect=AssertionError("Evaluación sin red")):
            report = evaluate(bundle,manifest,self.root/"run")
        self.assertEqual(report["status"],"INSUFFICIENT_EVIDENCE")
        self.assertEqual(report["ai_calls_during_evaluation"],0)
        self.assertEqual(report["holdout"]["status"],"NOT_CONSUMED")
        equity = json.loads((self.root/"run/validation_equity.json").read_text())
        self.assertEqual(len(equity["returns"]),146)
        self.assertTrue((self.root/"run/validation_orders.json").is_file())
        self.assertEqual(report["tests"]["benchmark"]["status"],"INSUFFICIENT_DATA")

    def test_holdout_outcomes_and_trace_files_without_ai(self):
        # Gates/metrics are forced solely to exercise orchestration, not profitability.
        from tick_engine import replay_ticks as real_replay
        bundle = translate(TEXT,translator=translator)
        path = self.manifest(bundle["document"])
        for outcome in ("passed", "failed", "missing_benchmark", "missing_trades"):
            with self.subTest(outcome=outcome), ExitStack() as stack:
                def simulate(data,h,cfg,execution,start,end,**kwargs):
                    result = real_replay(data,h,cfg,execution,start,end,**kwargs)
                    final = pd.Timestamp(start) >= pd.Timestamp("2019-08-08T00:00:00Z")
                    passive = kwargs.get("benchmark",False)
                    result.update(trades=100, net_return=.01 if passive else .1,
                                  sharpe=1 if passive else 2, forced_final=final)
                    if final and ((outcome=="missing_benchmark" and passive) or (outcome=="missing_trades" and not passive)):
                        result["trades"] = 0
                    return result
                stack.enter_context(patch("gherkin_pipeline.LEDGER_ROOT",self.root/outcome/"ledger"))
                stack.enter_context(patch("gherkin_pipeline.ask_model",side_effect=AssertionError("No AI")))
                stack.enter_context(patch("socket.socket.connect",side_effect=AssertionError("No network")))
                stack.enter_context(patch.dict("os.environ",{},clear=True))
                stack.enter_context(patch("tick_evaluation.replay_ticks",side_effect=simulate))
                for name in ("tick_walk_forward","monte_carlo_test"):
                    stack.enter_context(patch("tick_evaluation."+name,return_value={"passed":True,"status":"PASSED"}))
                stack.enter_context(patch("tick_evaluation.viable",side_effect=lambda r,*a: not (outcome=="failed" and r.get("forced_final"))))
                stack.enter_context(patch("tick_evaluation.deflated_sharpe",return_value={"dsr":.99,"dsr_z":10,"sequential_passed":True}))
                stack.enter_context(patch("gherkin_pipeline.mean_interval",return_value={"status":"ESTIMATED"}))
                report = evaluate(bundle,path,self.root/outcome/"run")
                expected = {"passed":"HISTORICAL_EVIDENCE","failed":"CRITERIA_NOT_MET"}.get(outcome,"INSUFFICIENT_EVIDENCE")
                self.assertEqual(report["status"],expected)
                self.assertEqual(report["holdout"]["passed"],outcome=="passed")
                for name in ("trades","orders","equity"):
                    self.assertTrue((self.root/outcome/"run"/f"holdout_{name}.json").is_file())
                again = evaluate(bundle,path,self.root/outcome/"again")
                self.assertEqual(again["status"],"HOLDOUT_ALREADY_USED")

    def test_contract_rejects_implicit_or_double_spread(self):
        for text in (TEXT.replace('"spread_bps": 0','"spread_bps": 2'),
                     TEXT.replace('"latency_ms": 0,',''),
                     TEXT.replace('"pip_size": null','"pip_size": 0.0001'),
                     TEXT.replace('"bars_per_year": 365','"bars_per_year": 252')):
            with self.assertRaises(ValueError): parse_markdown(text)

    def test_manifest_requires_explicit_matching_contract_and_coverage(self):
        document = parse_markdown(TEXT)
        manifest = self.manifest(document)
        spec = json.loads(manifest.read_text())
        spec["datasets"][0]["settings"]["slippage_bps"] = 2
        manifest.write_text(json.dumps(spec))
        with self.assertRaisesRegex(ValueError,"costos"):
            load_tick_manifest(manifest,document["contract"])

    def test_causality_future_mutation_and_bar_prefix(self):
        from tick_engine import tick_signals
        from tick_evaluation import causal_ticks
        from test_ticks import ticks,rule,execution
        f = ticks([(99,100),(102,103),(103,104),(99,100),(105,106),(102,103)],
                  pd.date_range("2020-01-01T00:00:00Z",periods=6,freq="min").tolist())
        h = rule(timeframe="1m",program={"entry":"col('close') > 100","exit":"col('close') < 100","parameters":[]})
        self.assertTrue(causal_ticks(f,h,execution())["passed"])

    def test_dashboard_tick_evaluation_is_offline(self):
        from dashboard import JobManager
        bundle = translate(TEXT,translator=translator)
        path = self.manifest(bundle["document"])
        manager = JobManager(self.root/"jobs")
        with patch("gherkin_pipeline.ask_model",side_effect=AssertionError("No AI")):
            manager.start_gherkin({"action":"evaluate","artifact":json.dumps(bundle),"data_manifest":str(path)})
            manager.thread.join(timeout=30)
        self.assertFalse(manager.busy)
        self.assertFalse(manager.error)
        self.assertEqual(manager.snapshot()["gherkin"]["execution_model"],"tick_bid_ask_v1")

    def test_dashboard_translation_preserves_artifact_numbers(self):
        from dashboard import JobManager
        bundle = translate(TEXT,translator=translator)
        manager = JobManager(self.root/"jobs")
        with patch("gherkin_pipeline.translate",return_value=bundle):
            manager.start_gherkin({"action":"translate","markdown":TEXT})
            manager.thread.join(timeout=30)
        self.assertFalse(manager.busy)
        self.assertFalse(manager.error)
        artifact = manager.snapshot()["gherkin"]["artifact"]
        self.assertIsInstance(artifact,str)
        # JSON transport keeps this string opaque, including decimal notation.
        transported = json.loads(json.dumps({"artifact":artifact}))["artifact"]
        self.assertEqual(load_bundle(json.loads(transported)),bundle["document"])

    def test_reserve_failure_is_consumed_and_not_retried(self):
        from tick_engine import replay_ticks as real_replay
        bundle = translate(TEXT,translator=translator)
        path = self.manifest(bundle["document"])
        def simulate(data,h,cfg,execution,start,end,**kwargs):
            if pd.Timestamp(start) >= pd.Timestamp("2019-08-08T00:00:00Z"):
                raise RuntimeError("holdout interrupted")
            result = real_replay(data,h,cfg,execution,start,end,**kwargs)
            result.update(trades=100,net_return=.1 if not kwargs.get("benchmark") else .01,
                          sharpe=2 if not kwargs.get("benchmark") else 1)
            return result
        with ExitStack() as stack:
            stack.enter_context(patch("tick_evaluation.replay_ticks",side_effect=simulate))
            for name in ("tick_walk_forward","monte_carlo_test"):
                stack.enter_context(patch("tick_evaluation."+name,return_value={"passed":True,"status":"PASSED"}))
            stack.enter_context(patch("tick_evaluation.viable",return_value=True))
            stack.enter_context(patch("tick_evaluation.deflated_sharpe",return_value={"dsr":.99,"dsr_z":10,"sequential_passed":True}))
            stack.enter_context(patch("gherkin_pipeline.mean_interval",return_value={"status":"ESTIMATED"}))
            with self.assertRaisesRegex(RuntimeError,"holdout interrupted"):
                evaluate(bundle,path,self.root/"first")
            saved=json.loads((self.root/"first/report.json").read_text())
            self.assertEqual(saved["holdout"]["status"],"CONSUMED_PENDING")
            second=evaluate(bundle,path,self.root/"second")
            self.assertEqual(second["status"],"HOLDOUT_ALREADY_USED")


if __name__ == "__main__": unittest.main()
