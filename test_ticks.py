import copy
import unittest
from dataclasses import replace

import pandas as pd

from tick_data import TickExecution, validate_ticks
from tick_engine import replay_ticks, tick_signals
from trading_agent import Settings


def execution(**changes):
    return TickExecution(**{**dict(signal_price="mid", distance_unit="price", pip_size=None,
        entry_order="market", entry_limit_offset=0, order_ttl_ms=10000,
        target_order="limit", latency_ms=0, holding_unit="milliseconds",
        max_quote_gap_ms=86400000, return_interval="1d", liquidate_at_end=True), **changes})


def rule(**changes):
    return {**dict(direction="long", timeframe="tick", allocation=.5, stop_loss=5,
                   take_profit=10, max_holding=100000, slow=1,
                   program={"entry": "col('bid') == 99", "exit": "False", "parameters": []}), **changes}


def ticks(quotes, times=None):
    times = times or pd.date_range("2020-01-01T00:00:00Z", periods=len(quotes), freq="s")
    return validate_ticks(pd.DataFrame({"timestamp": times, "sequence": range(len(quotes)),
                                      "bid": [q[0] for q in quotes], "ask": [q[1] for q in quotes]}))


CFG = Settings(capital=1000, commission_rate=0, fixed_commission=0, spread_bps=0,
               slippage_bps=0, min_notional=0, quantity_step=1, bars_per_year=365, allow_short=True)


class TickTests(unittest.TestCase):
    def run_ticks(self, quotes, h=None, ex=None, cfg=CFG, times=None):
        return replay_ticks(ticks(quotes, times), h or rule(), cfg, ex or execution())

    def test_stop_gap_fills_available_bid_on_next_event(self):
        r = self.run_ticks([(99,100), (99,100), (94,95), (90,91)])
        t = r["trade_log"][0]
        self.assertEqual(t["entry_price"], 100)
        self.assertEqual(t["exit_price"], 90)
        self.assertEqual(t["exit_reason"], "stop")
        self.assertEqual(t["quantity"], 5)
        self.assertEqual(r["final_equity"], 950)

    def test_target_limit_fills_improved_quote_and_wins_before_later_stop(self):
        r = self.run_ticks([(99,100), (99,100), (112,113), (80,81)])
        self.assertEqual(r["trade_log"][0]["exit_price"], 112)
        self.assertEqual(r["trade_log"][0]["exit_reason"], "target_limit")
        self.assertEqual(r["final_equity"], 1060)

    def test_market_target_keeps_trigger_when_market_reverses(self):
        r = self.run_ticks([(99,100), (99,100), (112,113), (104,105)], ex=execution(target_order="market"))
        self.assertEqual(r["trade_log"][0]["exit_price"], 104)
        self.assertEqual(r["trade_log"][0]["exit_reason"], "target")

    def test_short_stop_uses_ask(self):
        r = self.run_ticks([(99,100), (100,101), (103,106), (104,108)], h=rule(direction="short"))
        self.assertEqual(r["trade_log"][0]["entry_price"], 100)
        self.assertEqual(r["trade_log"][0]["exit_price"], 108)
        self.assertEqual(r["final_equity"], 960)

    def test_latency_prevents_early_fill(self):
        r = self.run_ticks([(99,100), (100,101), (101,102), (102,103)], ex=execution(latency_ms=1500))
        self.assertEqual(r["trade_log"][0]["entry_price"], 102)
        self.assertEqual(r["trade_log"][0]["entry_sequence"], 2)

    def test_limit_entry_waits_for_ask_and_expires(self):
        r = self.run_ticks([(99,100), (99,100), (97,98), (100,101)], ex=execution(entry_order="limit", entry_limit_offset=2))
        self.assertEqual(r["trade_log"][0]["entry_price"], 98)
        expired = self.run_ticks([(99,100), (99,100), (97,98)], ex=execution(entry_order="limit", entry_limit_offset=2, order_ttl_ms=1000))
        self.assertEqual(expired["trades"], 0)

    def test_same_time_ordering_and_duplicates(self):
        time = ["2020-01-01T00:00:00Z"] * 3
        r = self.run_ticks([(99,100), (99,100), (112,113)], times=time)
        self.assertEqual(r["trade_log"][0]["entry_sequence"], 1)
        f = ticks([(99,100), (99,100)], time[:2])
        f.loc[1,"sequence"] = 0
        with self.assertRaises(ValueError): validate_ticks(f)

    def test_future_quotes_do_not_change_prior_signals(self):
        f = ticks([(99,100), (100,101), (101,102), (102,103)])
        h = rule(program={"entry": "col('bid') > sma(col('bid'), 2)", "exit": "col('bid') < 100", "parameters": []})
        full = tick_signals(f, h, execution())
        prefix = tick_signals(f.iloc[:3], h, execution())
        self.assertEqual(full[0][:3].tolist(), prefix[0].tolist())

    def test_closed_bar_signal_not_visible_before_next_bar(self):
        f = ticks([(99,100),(101,102),(104,105),(106,107)],
                  ["2020-01-01T00:00:00Z","2020-01-01T00:00:30Z","2020-01-01T00:01:00Z","2020-01-01T00:01:01Z"])
        h = rule(timeframe="1m", program={"entry":"col('close') > 100","exit":"False","parameters":[]})
        en, _, decisions = tick_signals(f,h,execution())
        self.assertEqual(en.tolist(), [False,False,True,False])
        self.assertEqual(decisions.tolist(), [False,False,True,False])

    def test_return_sample_count_depends_on_clock_not_tick_count(self):
        r = self.run_ticks([(99,100),(99,100),(101,102),(101,102)])
        self.assertEqual(len(r["returns"]), 1)
        self.assertGreater(r["max_drawdown"], 0)

    def test_bad_quotes_and_gaps_are_rejected(self):
        with self.assertRaises(ValueError): ticks([(101,100),(99,100)])
        with self.assertRaises(ValueError):
            self.run_ticks([(99,100),(99,100)], ex=execution(max_quote_gap_ms=500))

    def test_exposure_measures_elapsed_time_and_ignores_redundant_quotes(self):
        times=["2020-01-01T00:00:00Z","2020-01-01T00:00:01Z","2020-01-01T00:00:02Z","2020-01-01T00:01:40Z"]
        r=self.run_ticks([(99,100),(99,100),(112,113),(80,81)],times=times)
        self.assertAlmostEqual(r["exposure"],.01)
        times.insert(3,"2020-01-01T00:00:50Z")
        more=self.run_ticks([(99,100),(99,100),(112,113),(80,81),(80,81)],times=times)
        self.assertAlmostEqual(r["exposure"],more["exposure"])

    def test_expired_passive_entry_is_insufficient_benchmark(self):
        from tick_evaluation import compare_benchmark
        f=ticks([(99,100),(99,100),(99,100),(99,100),(112,113)],
                ["2020-01-01T00:00:00Z","2020-01-01T00:00:20Z","2020-01-01T00:00:21Z","2020-01-01T00:00:22Z","2020-01-01T00:00:23Z"])
        active=replay_ticks(f,rule(),CFG,execution())
        passive=replay_ticks(f,rule(),CFG,execution(),benchmark=True)
        self.assertEqual(passive["trades"],0)
        self.assertEqual(compare_benchmark(active,passive)["status"],"INSUFFICIENT_DATA")

    def test_slippage_does_not_fill_a_limit_beyond_its_price(self):
        r=self.run_ticks([(99,100),(99,100),(111,112),(113,114)],cfg=replace(CFG,slippage_bps=100))
        self.assertEqual(r["trade_log"][0]["exit_sequence"],3)
        self.assertAlmostEqual(r["trade_log"][0]["exit_price"],111.87)

    def test_commission_and_calendar_carry_reconcile_cash(self):
        r=self.run_ticks([(99,100),(99,100),(110,111)],cfg=replace(CFG,commission_rate=.01,holding_cost_bps=100),
                         ex=execution(order_ttl_ms=200000000),
                         times=pd.date_range("2020-01-01T00:00:00Z",periods=3,freq="D").tolist())
        self.assertAlmostEqual(r["final_equity"],1027.62)
        self.assertAlmostEqual(sum(t["pnl"] for t in r["trade_log"]),r["final_equity"]-CFG.capital)

    def test_pips_percent_and_holding_events(self):
        for ex,h in ((execution(distance_unit="pips",pip_size=.1),rule(stop_loss=50)),
                     (execution(distance_unit="percent"),rule(stop_loss=.05,take_profit=.1))):
            r=self.run_ticks([(99,100),(99,100),(94,95),(90,91)],h=h,ex=ex)
            self.assertEqual(r["trade_log"][0]["exit_reason"],"stop")
        r=self.run_ticks([(99,100),(99,100),(100,101),(100,101)],h=rule(max_holding=1),ex=execution(holding_unit="signal_events"))
        self.assertEqual(r["trade_log"][0]["exit_reason"],"max_holding")

    def test_exit_signal_and_explicit_time_limit(self):
        h=rule(program={"entry":"col('bid') == 99","exit":"col('bid') == 101","parameters":[]})
        r=self.run_ticks([(99,100),(99,100),(101,102),(102,103)],h=h)
        self.assertEqual(r["trade_log"][0]["exit_reason"],"condition")
        r=self.run_ticks([(99,100),(99,100),(100,101),(100,101)],h=rule(max_holding=500))
        self.assertEqual(r["trade_log"][0]["exit_reason"],"max_holding")

    def test_pending_entry_cancelled_by_exit_and_unaffordable_orders(self):
        h=rule(program={"entry":"col('bid') == 99","exit":"col('bid') == 100","parameters":[]})
        r=self.run_ticks([(99,100),(100,101),(98,99)],h=h)
        self.assertEqual(r["trades"],0)
        r=self.run_ticks([(99,100),(99,100),(100,101)],cfg=replace(CFG,min_notional=10000))
        self.assertEqual(r["skipped_orders"],1)

    def test_short_insolvency_stops_replay(self):
        r=self.run_ticks([(99,100),(100,101),(1000,1001),(2000,2001)],h=rule(direction="short"))
        self.assertTrue(r["bankrupt"])
        self.assertEqual(r["trade_log"][0]["exit_reason"],"insolvency")

    def test_invalid_input_modes_and_segments(self):
        f=ticks([(99,100),(99,100),(100,101)])
        for kwargs in ({"cost_multiplier":0},{"signal_delay_ms":-1},
                       {"start":"2020-01-01T00:00:02Z","end":"2020-01-01T00:00:01Z"},
                       {"start":"2020-01-02T00:00:00Z"}):
            with self.assertRaises(ValueError): replay_ticks(f,rule(),CFG,execution(),**kwargs)
        with self.assertRaises(ValueError): replay_ticks(f,rule(direction="short"),replace(CFG,allow_short=False),execution())
        for changes in ({"pip_size":1},{"entry_limit_offset":1},{"latency_ms":10000},{"distance_unit":"pips"}):
            with self.assertRaises(ValueError): execution(**changes)
        with self.assertRaises(ValueError): replay_ticks(f,rule(stop_loss=200),CFG,execution())

    def test_tick_schema_rejects_bad_order_timezone_and_fields(self):
        f=ticks([(99,100),(99,100)])
        extra=f.copy(); extra["volume"]=1
        fractional=f.copy(); fractional["sequence"]=[0.,1.5]
        naive=f.copy(); naive["timestamp"]=["2020-01-01","2020-01-02"]
        missing=f.copy(); missing.loc[0,"bid"]=float('nan')
        for frame in (extra,f.iloc[::-1],fractional,naive,missing):
            with self.assertRaises(ValueError): validate_ticks(frame)

    def test_cancellation_and_partial_first_candle(self):
        with self.assertRaisesRegex(RuntimeError,"cancel"):
            replay_ticks(ticks([(99,100),(99,100)]),rule(),CFG,execution(),checkpoint=lambda: (_ for _ in ()).throw(RuntimeError("cancel")))
        f=ticks([(99,100),(99,100)], ["2020-01-01T00:00:01Z","2020-01-01T00:00:02Z"])
        h=rule(timeframe="1m",program={"entry":"True","exit":"False","parameters":[]})
        self.assertFalse(tick_signals(f,h,execution())[0].any())


if __name__ == "__main__": unittest.main()
