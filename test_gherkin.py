import copy
import json
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from gherkin_spec import SpecError, parse_markdown, run_scenarios
from gherkin_pipeline import Translation, evaluate, load_bundle, translate
from strategy_rules import RuleProgram


TEXT = (Path(__file__).parent / "examples/strategy-gherkin.md").read_text("utf-8")
PROGRAM = {"entry": "col('close') > 1", "exit": "col('close') < 1", "parameters": []}


def translator(document, model, key):
    return Translation(missing_details=[], unsupported=[], program=RuleProgram(**PROGRAM),
                       entry_source=document["rules"][0]["text"], exit_source=document["rules"][1]["text"])


class GherkinTests(unittest.TestCase):
    def setUp(self):
        self.ledger_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.ledger_tmp.cleanup)
        patcher = patch("gherkin_pipeline.LEDGER_ROOT", Path(self.ledger_tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_example_runs_real_signals_and_trades(self):
        doc = parse_markdown(TEXT)
        results = run_scenarios(doc, PROGRAM)
        self.assertEqual(len(results), 2)
        self.assertAlmostEqual(results[1]["actual"]["final_equity"], 980)

    def test_incorrect_translation_blocked_before_evaluation(self):
        with self.assertRaisesRegex(SpecError, "expresión normativa"):
            run_scenarios(parse_markdown(TEXT), {**PROGRAM, "entry": "col('close') >= 1"})

    def test_natural_language_fixture_catches_wrong_boundary(self):
        doc = parse_markdown(TEXT)
        for rule in doc["rules"]:
            rule.pop("expression")
        with self.assertRaisesRegex(SpecError, "Igualdad"):
            run_scenarios(doc, {**PROGRAM, "entry": "col('close') >= 1"})

    def test_unknown_step_is_not_ignored(self):
        with self.assertRaises(SpecError):
            parse_markdown(TEXT.replace("Cuando calculo las señales", "Cuando optimizo los parámetros"))

    def test_intrabar_blocked_before_model_call(self):
        with patch("gherkin_pipeline.ask_model") as model:
            with self.assertRaisesRegex(SpecError, "Capacidades"):
                translate(TEXT.replace("bar_open_market; stops_at_signal_close; estimated_fills", "ticks_intrabar"))
            model.assert_not_called()

    def test_missing_contract_and_duplicate_json_fail(self):
        with self.assertRaises(ValueError):
            parse_markdown(TEXT.replace('"asset": "EURUSD",', '"asset": "EURUSD", "asset":"BTCUSD",'))
        with self.assertRaises(ValueError):
            parse_markdown(TEXT.replace('"stop_loss": 0.1,', ''))

    def test_bundle_roundtrip_no_model_and_tamper_detection(self):
        bundle = translate(TEXT, translator=translator)
        with patch("gherkin_pipeline.ask_model", side_effect=AssertionError("IA prohibida")):
            self.assertEqual(load_bundle(bundle)["contract"]["asset"], "EURUSD")
        bundle["program"]["entry"] = "True"
        with self.assertRaisesRegex(SpecError, "Hash"):
            load_bundle(bundle)

    def test_engine_changes_are_rejected(self):
        bundle = translate(TEXT, translator=translator)
        with patch("gherkin_pipeline.runtime", return_value={}):
            with self.assertRaisesRegex(SpecError, "motor"):
                load_bundle(bundle)

    def test_expected_answers_not_passed_to_model(self):
        # The actual prompt construction is covered by a fake client; no network.
        from gherkin_pipeline import ask_model
        doc = parse_markdown(TEXT)
        with patch("langchain_openai.ChatOpenAI") as client:
            ask_model(doc, "model", "fake-key")
            prompt = client.return_value.with_structured_output.return_value.invoke.call_args.args[0]
            self.assertNotIn("final_equity", prompt)
            self.assertNotIn("2020-01-03", prompt)

    def manifest(self, folder, doc, frame=None):
        c = doc["contract"]
        if frame is None:
            times = pd.date_range(c["evaluation"]["start"], c["evaluation"]["end"], freq="D", inclusive="left")
            # Flat below entry => no trades: must not report profitability.
            frame = pd.DataFrame({"timestamp": times, "open": .9, "high": .9, "low": .9, "close": .9})
        frame.to_csv(folder / "market.csv", index=False)
        spec = {k: c[k] for k in ("asset", "source", "contract", "quote_currency", "instrument_type")}
        spec.update(file="market.csv", timeframe=c["data_timeframe"], settings=c["costs"])
        path = folder / "manifest.json"
        path.write_text(json.dumps({"datasets": [spec]}), encoding="utf-8")
        return path

    def test_evaluate_without_ai_or_credentials_and_reproducible(self):
        bundle = translate(TEXT, translator=translator)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            manifest = self.manifest(folder, bundle["document"])
            with patch("gherkin_pipeline.ask_model", side_effect=AssertionError("IA prohibida")), patch.dict("os.environ", {}, clear=True):
                a = evaluate(bundle, manifest, folder / "one")
                b = evaluate(bundle, manifest, folder / "another_parent" / "two")
            self.assertEqual(a["status"], "INSUFFICIENT_EVIDENCE")
            self.assertEqual(a["out_of_sample"], b["out_of_sample"])
            self.assertEqual(a["holdout"]["status"], "NOT_CONSUMED")
            self.assertEqual(b["trials"], a["trials"] + 1)
            self.assertTrue((folder / "one/report.html").is_file())

    def test_cost_mismatch_blocks_run(self):
        bundle = translate(TEXT, translator=translator)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            doc = copy.deepcopy(bundle["document"])
            doc["contract"]["costs"]["spread_bps"] = 5
            manifest = self.manifest(folder, doc)
            with self.assertRaisesRegex(SpecError, "costos"):
                evaluate(bundle, manifest, folder / "run")
            self.assertFalse((folder / "run").exists())

    def test_observed_data_cannot_become_fresh_holdout(self):
        from experiment_protocol import HoldoutLedger
        ledger = HoldoutLedger(Path(self.ledger_tmp.name) / "observed.sqlite3")
        ledger.observe("2018-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "first")
        self.assertFalse(ledger.reserve("2021-01-01T00:00:00Z", "2023-01-01T00:00:00Z", "second", "different-strategy"))
        self.assertTrue(ledger.reserve("2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "third", "fresh"))

    def test_ledger_compares_actual_times_and_migrates_legacy_rows(self):
        import sqlite3
        from experiment_protocol import HoldoutLedger
        path = Path(self.ledger_tmp.name) / "offsets.sqlite3"
        ledger = HoldoutLedger(path)
        ledger.observe("2023-12-31T00:00:00Z", "2024-01-01T00:00:00Z", "first")
        self.assertFalse(ledger.reserve("2024-01-01T01:00:00+02:00", "2024-01-02T00:00:00Z", "second", "overlap"))
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("INSERT INTO holdouts(start,end,run,signature) VALUES (?,?,?,?)",
                       ("2025-01-01T01:00:00+02:00", "2025-01-02T00:00:00Z", "old", "old"))
        migrated = HoldoutLedger(path)
        self.assertFalse(migrated.reserve("2024-12-31T23:30:00Z", "2025-01-01T00:00:00Z", "new", "overlap"))

    def test_dashboard_evaluation_without_model(self):
        from dashboard import JobManager
        bundle = translate(TEXT, translator=translator)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            manifest = self.manifest(folder, bundle["document"])
            manager = JobManager(folder / "jobs")
            with patch("gherkin_pipeline.ask_model", side_effect=AssertionError("IA prohibida")), patch.dict("os.environ", {}, clear=True):
                manager.start_gherkin({"action": "evaluate", "artifact": bundle, "data_manifest": str(manifest)})
                manager.thread.join(timeout=30)
            self.assertFalse(manager.busy)
            self.assertFalse(manager.error)
            self.assertEqual(manager.snapshot()["gherkin"]["status"], "INSUFFICIENT_EVIDENCE")

    def test_free_descriptions_and_unknown_tags_are_blocked(self):
        for text in (TEXT.replace("  Antecedentes:", "  Operar solo los lunes\n  Antecedentes:"),
                     TEXT.replace("  Escenario: Igualdad", "  @ignorar_costos\n  Escenario: Igualdad")):
            with self.assertRaises(SpecError):
                parse_markdown(text)

    def test_final_failure_consumes_reserve_before_computation(self):
        # Orchestration fixture only: forced gates are not evidence of profitability.
        from experiment_protocol import HoldoutLedger
        from trading_agent import backtest as real_backtest
        bundle = translate(TEXT, translator=translator)
        def fake_backtest(*args, **kwargs):
            result = real_backtest(*args, **kwargs)
            result.update(trades=100, net_return=.1, sharpe=2)
            return result
        def fail_final(*args, **kwargs):
            ledger = HoldoutLedger(Path(self.ledger_tmp.name) / "holdout_ledger.sqlite3")
            self.assertIsNotNone(ledger.latest_end())
            raise RuntimeError("interrupción posterior a reserva")
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            folder = Path(tmp)
            manifest = self.manifest(folder, bundle["document"])
            stack.enter_context(patch("gherkin_pipeline.backtest", side_effect=fake_backtest))
            stack.enter_context(patch("gherkin_pipeline.viable", return_value=True))
            for name in ("walk_forward_test", "monte_carlo_test", "benchmark_test"):
                stack.enter_context(patch("gherkin_pipeline." + name, return_value={"passed": True}))
            stack.enter_context(patch("gherkin_pipeline.deflated_sharpe", return_value={"dsr": .99, "dsr_z": 10, "sequential_passed": True}))
            stack.enter_context(patch("gherkin_pipeline.mean_interval", return_value={"status": "ESTIMATED", "lower": .001, "upper": .002}))
            final = stack.enter_context(patch("experiment_protocol.evaluate_frozen", side_effect=fail_final))
            with self.assertRaisesRegex(RuntimeError, "posterior a reserva"):
                evaluate(bundle, manifest, folder / "first")
            saved = json.loads((folder / "first/report.json").read_text("utf-8"))
            self.assertEqual(saved["holdout"]["status"], "CONSUMED_PENDING")
            second = evaluate(bundle, manifest, folder / "different_parent" / "second")
            self.assertEqual(second["status"], "HOLDOUT_ALREADY_USED")
            self.assertEqual(final.call_count, 1)

    def test_http_gherkin_endpoints_require_session_and_serve_artifacts(self):
        import re
        import threading
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        from dashboard import JobManager, create_server
        from gherkin_pipeline import save_translation
        bundle = translate(TEXT, translator=translator)
        with tempfile.TemporaryDirectory() as tmp:
            manager = JobManager(Path(tmp))
            server = create_server(0, manager)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urlopen(base) as response:
                    page = response.read().decode()
                token = re.search(r'name="session-token" content="([^"]+)"', page).group(1)
                with urlopen(base + "/api/gherkin/example") as response:
                    self.assertEqual(response.read().decode(), TEXT)
                with urlopen(base + "/api/gherkin/example-ticks") as response:
                    self.assertIn('tick_bid_ask_v1', response.read().decode())
                payload = json.dumps({"action": "translate", "markdown": TEXT}).encode()
                with self.assertRaises(HTTPError) as denied:
                    urlopen(Request(base + "/api/gherkin/start", data=payload, headers={"Content-Type": "application/json"}))
                self.assertEqual(denied.exception.code, 403)
                with patch.object(manager, "start_gherkin") as start:
                    with urlopen(Request(base + "/api/gherkin/start", data=payload,
                                         headers={"Content-Type": "application/json", "X-Session-Token": token})) as response:
                        self.assertEqual(response.status, 202)
                    start.assert_called_once()
                manager.output = Path(tmp) / "translation"
                save_translation(bundle, manager.output)
                manager.gherkin_result = {"status": "TRANSLATED"}
                with urlopen(base + "/api/gherkin/download/strategy.json") as response:
                    self.assertEqual(json.loads(response.read())["sha256"], bundle["sha256"])
                (manager.output / "validation_trades.json").write_text("[]")
                with urlopen(base + "/api/gherkin/download/validation_trades.json") as response:
                    self.assertEqual(json.loads(response.read()), [])
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
