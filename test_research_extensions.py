import json
import sqlite3
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

import numpy as np
import pandas as pd

from execution import scheduled_signals, signal_frame, causal_signal_audit, execution_capabilities
from market_data import load_market_manifest
from research import ResearchBrief, validate_brief
from research_memory import ResearchMemory
from strategy_rules import program_signals
from trading_agent import Settings, backtest, signals, strategy_source, compact, hypothesis_signature, demo_data
from validation import joint_parameter_variants
from verify_execution import compare_frames, verify
from mql5_export import export_mql5
from test_research import brief, evidence
from test_strategy_rules import hypothesis


def bars(prices, frequency="D"):
    prices = np.asarray(prices, float)
    return pd.DataFrame({"timestamp": pd.date_range("2020-01-01", periods=len(prices), freq=frequency, tz="UTC"),
                         "open": prices, "high": prices*1.001, "low": prices*.999, "close": prices})


def rule(**updates):
    return {**hypothesis("True", "False").model_dump(), "allocation": 1., "max_holding": 1000,
            "stop_loss": .9, "take_profit": 10., **updates}


def frictionless(**updates):
    return Settings(commission_rate=0, spread_bps=0, slippage_bps=0, min_notional=0,
                    quantity_step=1, **updates)


class ExecutionExtensions(unittest.TestCase):
    def test_short_profit_and_accounting(self):
        data = bars([100, 100, 90, 80])
        result = backtest(data, rule(direction="short"), frictionless(allow_short=True), 1000)
        self.assertEqual(result["final_equity"], 1200)
        self.assertEqual(result["trade_log"][0]["pnl"], 200)
        self.assertEqual(result["trades"], 1)
        with self.assertRaisesRegex(ValueError, "short"):
            backtest(data, rule(direction="short"), frictionless(), 1000)

    def test_short_stop_executes_next_open_with_gap(self):
        data = bars([100, 100, 120, 150])
        result = backtest(data, rule(direction="short", stop_loss=.1), frictionless(allow_short=True), 1000)
        self.assertEqual(result["trade_log"][0]["exit_price"], 150)
        self.assertEqual(result["final_equity"], 500)

    def test_short_round_trip_costs(self):
        cfg = Settings(allow_short=True, commission_rate=.01, fixed_commission=1,
                       spread_bps=0, slippage_bps=0, min_notional=1, quantity_step=1)
        for direction in ("long", "short"):
            result = backtest(bars([100]*4), rule(direction=direction, allocation=.9), cfg, 1000)
            self.assertEqual(result["final_equity"], 982)
            self.assertEqual(result["trade_log"][0]["pnl"], -18)

    def test_dynamic_bid_ask_not_mid_fill(self):
        data = bars([100]*4)
        data["bid_open"] = data["bid_close"] = 99.
        data["ask_open"] = data["ask_close"] = 101.
        cfg = frictionless(allow_short=True)
        for direction in ("long", "short"):
            result = backtest(data, rule(direction=direction), cfg, 1000)
            trade = result["trade_log"][0]
            self.assertEqual(trade["entry_price"], 101 if direction == "long" else 99)
            self.assertEqual(trade["exit_price"], 99 if direction == "long" else 101)
            self.assertLess(result["final_equity"], 1000)
            stressed = backtest(data, rule(direction=direction), cfg, 1000, cost_multiplier=3)
            self.assertLess(stressed["final_equity"], result["final_equity"])

    def test_dynamic_cost_nan_and_partial_quotes_fail_closed(self):
        for column, value in (("spread_bps", np.nan), ("slippage_bps", -1)):
            data = bars([100]*4)
            data[column] = value
            with self.assertRaises(ValueError):
                backtest(data, rule(), frictionless(), 1000)
        data = bars([100]*4)
        data["bid_open"] = 99
        with self.assertRaises(ValueError):
            backtest(data, rule(), frictionless(), 1000)

    def test_intraday_carry_is_elapsed_time_not_one_day_per_bar(self):
        result = backtest(bars([100]*25, "h"), rule(timeframe="1h"), frictionless(holding_cost_bps=24), 1000)
        self.assertAlmostEqual(result["final_equity"], 1000-23*.1)

    def test_signal_5m_executes_on_1m_only_after_complete_close(self):
        data = bars(np.arange(12)+100, "min")
        h = rule(timeframe="5m")
        entry, _, available, _ = scheduled_signals(data, h, signals, program_signals)
        self.assertEqual(np.flatnonzero(available).tolist(), [4, 9])
        result = backtest(data, h, frictionless(), 1000)
        self.assertEqual(result["trade_log"][0]["entry_timestamp"], str(data.timestamp.iloc[5]))
        self.assertEqual(result["trade_log"][0]["entry_price"], 105)
        self.assertTrue(causal_signal_audit(data, h, signals, program_signals)["passed"])

    def test_missing_subbar_does_not_create_complete_signal_bar(self):
        data = bars([100]*15, "min").drop(index=2).reset_index(drop=True)
        frame = signal_frame(data, "5m")
        self.assertEqual(frame.timestamp.iloc[0], pd.Timestamp("2020-01-01T00:05Z"))

    def test_daily_cannot_invent_scalping_data(self):
        data = bars([100]*20)
        with self.assertRaisesRegex(ValueError, "NEEDS_CAPABILITY"):
            backtest(data, rule(timeframe="1m"), frictionless(), 1000)
        self.assertEqual(execution_capabilities(data)["signal_timeframes"], ["1d"])

    def test_confirmation_counts_complete_signal_bars_not_execution_rows(self):
        from experiment_protocol import confirmation_end
        data = bars([100]*600, "min")
        cfg = replace(frictionless(), forward_min_days=0)
        self.assertEqual(confirmation_end(data, rule(timeframe="5m"), cfg, 0), 600)
        self.assertIsNone(confirmation_end(data.drop(index=2).reset_index(drop=True), rule(timeframe="5m"), cfg, 0))

    def test_causality_handles_integer_features(self):
        data = bars([100]*40)
        data["volume"] = np.arange(40, dtype=int)
        self.assertTrue(causal_signal_audit(data, rule(), signals, program_signals)["passed"])

    def test_causality_detects_future_dependent_signal(self):
        data = bars(np.arange(40)+100)
        h = {**rule(), "program": None}
        def leaked(frame, hypothesis):
            return (frame.close > frame.close.mean()).to_numpy()
        self.assertFalse(causal_signal_audit(data, h, leaked, program_signals)["passed"])

    def test_intraday_dd_is_visible_when_close_recovers(self):
        data = bars([100]*4)
        data.loc[2, "low"] = 50
        result = backtest(data, rule(), frictionless(), 1000)
        self.assertEqual(result["max_drawdown"], 0)
        self.assertEqual(result["intrabar_drawdown_lower_bound"], .5)

    def test_direction_and_timeframe_change_dedup_signature(self):
        a = rule()
        self.assertNotEqual(hypothesis_signature(a), hypothesis_signature({**a, "direction": "short"}))
        self.assertNotEqual(hypothesis_signature(a), hypothesis_signature({**a, "timeframe": "1h"}))

    def test_exported_python_multiresolution_short_matches(self):
        import sys
        import types
        h, cfg = rule(timeframe="5m", direction="short"), frictionless(allow_short=True)
        module = types.ModuleType("multiframe_export_test")
        sys.modules[module.__name__] = module
        try:
            exec(compile(strategy_source(h, cfg, {}), "export.py", "exec"), module.__dict__)
            data = bars(np.arange(50)+100, "min")
            self.assertEqual(compact(backtest(data, h, cfg, 1000)), compact(module.backtest(data, h, module.Settings(**asdict(cfg)), 1000)))
        finally:
            del sys.modules[module.__name__]


class ResearchExtensions(unittest.TestCase):
    def test_original_conjecture_without_invented_citations(self):
        b = ResearchBrief.model_validate({**brief().model_dump(), "origin": "conjecture", "sources": []})
        validate_brief(b, evidence(), {"CUSTOM"})
        with self.assertRaises(ValueError):
            validate_brief(b.model_copy(update={"origin": "replication"}), evidence(), {"CUSTOM"})

    def test_short_compatibility_is_per_contract(self):
        b = brief().model_copy(update={"requires_shorting": True})
        validate_brief(b, evidence(), {"CUSTOM"}, capabilities={"CUSTOM": {"signal_timeframes": ["1d"], "directions": ["long", "short"]}})
        with self.assertRaises(ValueError):
            validate_brief(b, evidence(), {"CUSTOM"})

    def test_scalping_order_requirements_are_not_silently_approximated(self):
        for change in ({"order_type": "limit"}, {"requires_ticks": True}, {"requires_depth": True}, {"stop_evaluation": "intrabar"}):
            with self.assertRaisesRegex(ValueError, "NEEDS_CAPABILITY"):
                validate_brief(brief().model_copy(update=change), evidence(), {"CUSTOM"})

    def test_ledger_is_append_only_and_preserves_result_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"ledger.sqlite3"
            ledger = ResearchMemory(path)
            ledger.reserve("a", 1, "scope", "sig", "structure", {}, 5)
            ledger.save("a", 1, {"x": 1})
            ledger.save("a", 1, {"x": 2}, "REJECTED")
            self.assertEqual(ledger.verify()["events"], 4)
            with closing(sqlite3.connect(path)) as db:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("DELETE FROM audit_events")
                db.execute("DELETE FROM experiments")
                db.commit()
            self.assertEqual(ResearchMemory(path).reserve("b", 1, "newscope", "sig2", "structure", {}, 5), 5)

    def test_joint_perturbations_are_reproducible_and_change_multiple_parameters(self):
        h = rule()
        a = list(joint_parameter_variants(h, 8, 42))
        self.assertEqual(a, list(joint_parameter_variants(h, 8, 42)))
        self.assertTrue(a)
        self.assertTrue(all(sum(h[k] != variant[k] for k in h) >= 2 for variant in a))

    def test_local_contract_manifest_and_spot_short_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bars([100]*800, "min").to_csv(root/"bars.csv", index=False)
            spec = {"asset": "TESTUSD", "file": "bars.csv", "source": "broker export", "contract": "test CFD",
                    "quote_currency": "USD", "instrument_type": "cfd", "timeframe": "1m",
                    "settings": {"bars_per_year": 362880, "quote_side": 1, "commission_rate": .0001,
                                 "spread_bps": 2, "slippage_bps": 1, "quantity_step": 1, "min_notional": 1,
                                 "holding_cost_bps": 1, "allow_short": True}}
            path = root/"manifest.json"
            path.write_text(json.dumps({"datasets": [spec]}))
            data, metadata, profiles = load_market_manifest(path)
            self.assertEqual(len(data["TESTUSD"]), 800)
            self.assertTrue(profiles["TESTUSD"]["allow_short"])
            self.assertTrue(metadata["TESTUSD"]["sha256"])
            from trading_agent import TradingAgent
            agent = TradingAgent(data, Settings(), root/"run", demo=True, market_metadata=metadata, asset_settings=profiles)
            agent.use_asset("TESTUSD")
            self.assertEqual(agent.cfg.bars_per_year, 362880)
            self.assertIn("short", agent.research_context()["TESTUSD"]["capabilities"]["directions"])
            state = {"hypothesis": rule(timeframe="5m", direction="short"), "iteration_count": 1, "status": "RESEARCHING"}
            agent.prototyper_node(state)
            self.assertTrue((root/"run"/"prototype_01_TESTUSD.py").exists())
            spec["instrument_type"] = "spot"
            path.write_text(json.dumps({"datasets": [spec]}))
            with self.assertRaisesRegex(ValueError, "Spot"):
                load_market_manifest(path)

    def test_mql_short_timeframe_and_parity_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            data = bars(np.arange(80)+100, "h")
            h = rule(timeframe="1h", direction="short")
            cfg = frictionless(allow_short=True)
            bundle = export_mql5(h, "EURUSD", data, Path(folder)/"ea", hypothesis_signature(h), asdict(cfg))
            root = Path(bundle["directory"])
            source = (root/"strategy.mq5").read_text()
            self.assertIn("const int Direction=-1", source)
            self.assertIn("SignalTimeframe=PERIOD_H1", source)
            self.assertIn("trade.Sell", source)
            self.assertIn("WriteShadow", source)
            self.assertEqual(verify(root, root/"reference_signals.csv")["status"], "PARTIAL")
            self.assertEqual(verify(root, root/"reference_signals.csv", root/"reference_trades.csv")["status"], "MATCHED")
            observed = pd.read_csv(root/"reference_trades.csv")
            observed["pnl"] += 100
            observed.to_csv(root/"observed.csv", index=False)
            self.assertEqual(verify(root, root/"reference_signals.csv", root/"observed.csv")["status"], "FAILED")

    def test_parity_rejects_missing_and_duplicate_observations(self):
        frame = pd.DataFrame({"timestamp": [1, 2], "entry": [0, 1]})
        self.assertFalse(compare_frames(frame, frame.iloc[:1], ["timestamp"], [], ["entry"], 0)["passed"])
        self.assertFalse(compare_frames(frame, pd.concat([frame, frame]), ["timestamp"], [], ["entry"], 0)["passed"])


if __name__ == "__main__":
    unittest.main()
