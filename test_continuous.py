import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import numpy as np

from trading_agent import (Settings, TradingAgent, RunCancelled, Hypothesis, demo_data,
                           initial_state, variant_hypothesis, hypothesis_signature,
                           deflated_sharpe, signals)


class ContinuousTests(unittest.TestCase):
    def test_runs_past_ten_then_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop = threading.Event()
            def event(state):
                if state.get("report_count", 0) == 12:
                    stop.set()
            agent = TradingAgent(demo_data(), Settings(min_trades=100000), Path(tmp)/"run", demo=True,
                                 on_event=event, stop_requested=stop.is_set)
            state = agent.run()
            self.assertEqual(state["iteration_count"], 12)
            self.assertEqual(state["report_count"], 12)
            self.assertEqual(state["lifecycle"], "CANCELLED")
            self.assertTrue(all(r["quant_metrics"] for r in state["reports"]))
            self.assertFalse((agent.output/"production_strategy.py").exists())
            with closing(sqlite3.connect(agent.registry)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM hypotheses").fetchone()[0],12)

    def test_unlimited_routing_and_explicit_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent(demo_data(),Settings(max_iterations=0),Path(tmp)/"run",demo=True)
            state = initial_state()
            state.update(iteration_count=100001,status="REJECTED")
            self.assertEqual(agent.should_continue_after_report(state),"researcher_node")
            self.assertEqual(Settings(max_iterations=500).max_iterations,500)
            with self.assertRaises(ValueError): Settings(max_iterations=-1)

    def test_variants_are_new_and_causal(self):
        signatures = [hypothesis_signature(variant_hypothesis(i).model_dump()) for i in range(256)]
        self.assertEqual(len(set(signatures)),256)
        original = demo_data()
        changed = original.copy()
        changed.loc[500:,["open","high","low","close"]] *= 2
        for index in range(8):
            h=variant_hypothesis(index).model_dump()
            np.testing.assert_array_equal(signals(original,h)[:500],signals(changed,h)[:500])

    def test_duplicate_demo_configuration_gets_new_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent(demo_data(),Settings(max_iterations=0),Path(tmp)/"run",demo=True)
            h=variant_hypothesis(0)
            agent.register_hypothesis(h, 0)
            with patch("trading_agent.variant_hypothesis", side_effect=[h, variant_hypothesis(1), variant_hypothesis(2)]):
                state=initial_state()
                first=agent.researcher_node(state)
                state.update(first)
                second=agent.researcher_node(state)
            self.assertNotEqual(hypothesis_signature(first["hypothesis"]),hypothesis_signature(second["hypothesis"]))
            self.assertIn("reemplazada",first["logs"][-1])

    def test_statistics_tighten_without_limiting_execution(self):
        r=np.random.default_rng(2).normal(.001,.01,1000)
        first=deflated_sharpe(r,Settings(),attempt=1)
        later=deflated_sharpe(r,Settings(),attempt=101)
        self.assertEqual(first["trials_used"],100)
        self.assertEqual(later["trials_used"],101)
        self.assertGreater(later["sequential_z_min"],first["sequential_z_min"])
        self.assertLess(later["sequential_alpha"],first["sequential_alpha"])

    def test_stop_cancels_model_wait(self):
        cancelled=threading.Event()
        entered=threading.Event()
        stop=threading.Event()
        class SlowLLM:
            def with_structured_output(self,*args,**kwargs): return self
            async def ainvoke(self,*args,**kwargs):
                entered.set()
                try: await asyncio.sleep(60)
                finally: cancelled.set()
        with tempfile.TemporaryDirectory() as tmp:
            agent=TradingAgent(demo_data(),Settings(max_iterations=0),Path(tmp)/"run",demo=True,stop_requested=stop.is_set)
            agent.llm=SlowLLM()
            errors=[]
            def invoke():
                try: agent.call_model(Hypothesis,"test")
                except RunCancelled: errors.append("cancelled")
            thread=threading.Thread(target=invoke,daemon=True)
            thread.start()
            self.assertTrue(entered.wait(3))
            stop.set()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(cancelled.is_set())
            self.assertEqual(errors,["cancelled"])

    def test_report_memory_is_bounded_and_files_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent=TradingAgent(demo_data(),Settings(max_iterations=0),Path(tmp)/"run",demo=True)
            state=initial_state()
            state.update(status="REJECTED",hypothesis=variant_hypothesis(0).model_dump())
            for attempt in range(1,106):
                state["iteration_count"]=attempt
                state.update(agent.reporter_node(state))
            self.assertEqual(len(state["reports"]),100)
            self.assertEqual(state["report_count"],105)
            self.assertTrue((agent.output/"reports/attempt_01.json").exists())
            self.assertTrue((agent.output/"reports/attempt_105.json").exists())
            self.assertNotIn("Límite",state["latest_report"]["next_action"])

    def test_snapshot_retries_transient_windows_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent=TradingAgent(demo_data(),Settings(max_iterations=0),Path(tmp)/"run",demo=True)
            original=Path.replace
            calls=[]
            def transient(path,target):
                calls.append(1)
                if len(calls)<3:
                    raise PermissionError("Windows sharing violation")
                return original(path,target)
            with patch.object(Path,"replace",transient), patch("trading_agent.time.sleep"):
                agent.save(initial_state())
            self.assertEqual(len(calls),3)
            self.assertEqual(json.loads((agent.output/"state.json").read_text("utf-8"))["iteration_count"],0)


if __name__ == "__main__": unittest.main()
