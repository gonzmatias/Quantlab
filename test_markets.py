import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from market_data import ASSETS, decode_duka, invert_ohlc, download_asset
from trading_agent import TradingAgent, Settings, initial_state, demo_data, variant_hypothesis, market_settings
from report_agent import build_report, render_html


class MarketTests(unittest.TestCase):
    def test_inverse_ohlc_extremes(self):
        frame = pd.DataFrame(dict(open=[2.], close=[4.], high=[5.], low=[1.]))
        inverse = invert_ohlc(frame)
        self.assertEqual(inverse.iloc[0].to_dict(), dict(open=.5, close=.25, high=1., low=.2))

    def test_delta_decoder_does_not_fill_missing_days(self):
        payload = dict(timestamp=1704067200000, multiplier=.01, shift=86400000, times=[0,3],
                       open=1., high=2., low=.5, close=1.5, opens=[0,10], highs=[0,20], lows=[0,5], closes=[0,10])
        data = decode_duka(payload)
        self.assertEqual(len(data), 2)
        self.assertEqual((data.timestamp.iloc[1]-data.timestamp.iloc[0]).days, 3)
        self.assertAlmostEqual(data.close.iloc[-1], 1.6)

    def test_coinbase_filters_response_outside_requested_range(self):
        start, end = pd.Timestamp('2024-01-01', tz='UTC'), pd.Timestamp('2024-01-03', tz='UTC')
        payload = [[int(t.timestamp()), 1, 3, 2, 2, 100] for t in pd.date_range(start-pd.Timedelta(days=1),end)]
        data, urls = download_asset('BTCUSD', start, end, fetch=lambda *args: payload)
        self.assertEqual(len(data),2)
        self.assertTrue((data.timestamp < end).all())

    def test_all_assets_precede_next_hypothesis_and_trials_count_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = TradingAgent({k:demo_data(i) for i,k in enumerate(ASSETS)}, Settings(max_iterations=2,bootstrap_samples=50), Path(tmp)/'run', demo=True)
            events=[]
            original=a.researcher_node
            def research(state):
                events.append(('research',state['iteration_count']+1))
                return original(state)
            def quant(state):
                asset=next(k for k,v in a.datasets.items() if v is a.data)
                events.append((asset,a.quant_trial_index))
                return dict(quant_metrics={'passed':False,'out_of_sample':{'net_return':-.01,'sharpe':-1}},charts={},logs=state['logs'])
            with patch.object(a,'researcher_node',side_effect=research), patch.object(a,'_quant_single',side_effect=quant):
                result=a.run()
            self.assertEqual(events,[('research',1)]+list(zip(ASSETS,range(1,6)))+[('research',2)]+list(zip(ASSETS,range(6,11))))
            self.assertEqual(set(result['asset_results']),set(ASSETS))

    def test_stress_checks_every_quant_passer_then_ranks_survivors(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=TradingAgent({k:demo_data() for k in ASSETS},Settings(),Path(tmp)/'run',demo=True)
            rows={k:dict(quant_metrics={'passed':True,'out_of_sample':{'net_return':.1,'sharpe':5-i,'max_drawdown':.1}},stress_metrics={},charts={}) for i,k in enumerate(ASSETS)}
            s={**initial_state(),'iteration_count':1,'hypothesis':variant_hypothesis(0).model_dump(),'asset_results':rows,'history':[{}]}
            visited=[]
            def stress(state):
                asset=next(k for k,v in a.datasets.items() if v is a.data)
                visited.append(asset)
                return dict(stress_metrics={'passed':asset=='BTCUSD'},charts={},logs=[])
            with patch.object(a,'_stress_single',side_effect=stress):
                result=a.stress_test_node(s)
            self.assertEqual(visited,list(ASSETS))
            self.assertEqual(result['selected_asset'],'BTCUSD')
            self.assertTrue(result['stress_metrics']['passed'])

    def test_positive_does_not_mean_approved_and_report_names_assets(self):
        state={**initial_state(),'selected_asset':'EURUSD','asset_results':{'EURUSD':{'quant_metrics':{'passed':False,'out_of_sample':{'net_return':.1}},'stress_metrics':{}}}}
        report=build_report(state,Settings().__dict__,False)
        self.assertEqual(report['outcome'],'REJECTED')
        self.assertEqual(report['positive_assets'],['EURUSD'])
        self.assertIn('POSITIVO',render_html(report))

    def test_execution_profiles_and_unknown_override(self):
        cfg=Settings()
        self.assertEqual(market_settings(cfg,'CADUSD').quote_side,2)
        self.assertEqual(market_settings(cfg,'EURUSD').quantity_step,1000)
        self.assertEqual(market_settings(cfg,'BTCUSD').bars_per_year,365)
        self.assertEqual(market_settings(cfg,'BTCUSD',{'fixed_commission':2}).fixed_commission,2)
        with self.assertRaises(ValueError):
            market_settings(cfg,'BTCUSD',{'bars_per_year':1})


if __name__ == '__main__':
    unittest.main()
