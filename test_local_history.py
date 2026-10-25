import json
import lzma
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from local_history import HistoryStore, HistoryError, load_local_contract
from dukascopy_history import decode_hour, download_history


DAY = pd.Timestamp('2024-01-02T00:00:00Z')


def quotes(day=DAY):
    times = pd.date_range(day, day+pd.Timedelta(days=1),freq='min',inclusive='left')
    return pd.DataFrame(dict(timestamp=times, sequence=range(len(times)), bid=1.1, ask=1.1001))


def bi5():
    return lzma.compress(b''.join(struct.pack('>IIIff',ms,110010,110000,1.,2.) for ms in range(0,3600000,60000)))


class LocalHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_parquet_and_duckdb_read_exact_subset_offline(self):
        store = HistoryStore(self.root)
        store.put_day('EURUSD',DAY,quotes(),{'test_fixture': True},[])
        with patch('socket.socket.connect',side_effect=AssertionError('offline')):
            data, meta = store.read('EURUSD',DAY+pd.Timedelta(hours=1),DAY+pd.Timedelta(hours=2))
        self.assertEqual(len(data),60)
        self.assertEqual(data.sequence.iloc[0],60)
        self.assertEqual(meta['storage'],'parquet_duckdb_v1')
        self.assertTrue((self.root/'catalog.duckdb').is_file())
        self.assertEqual(len(list(self.root.rglob('*.parquet'))),1)

    def test_missing_coverage_and_quality_rejected(self):
        store=HistoryStore(self.root)
        store.put_day('EURUSD',DAY,quotes(),{},['missing_hour:04'])
        with self.assertRaisesRegex(HistoryError,'calidad'):
            store.read('EURUSD',DAY,DAY+pd.Timedelta(days=1))
        with self.assertRaisesRegex(HistoryError,'Falta'):
            store.read('EURUSD',DAY,DAY+pd.Timedelta(days=2))

    def test_tampering_and_overwrite_rejected(self):
        store=HistoryStore(self.root)
        store.put_day('EURUSD',DAY,quotes(),{},[])
        with self.assertRaisesRegex(HistoryError,'existe'):
            store.put_day('EURUSD',DAY,quotes(),{},[])
        path=next(self.root.rglob('*.parquet'))
        path.write_bytes(path.read_bytes()+b'changed')
        with self.assertRaisesRegex(HistoryError,'hash'):
            store.read('EURUSD',DAY,DAY+pd.Timedelta(days=1))

    def test_bi5_scale_sequence_and_order(self):
        frame=decode_hour(bi5(),'EURUSD',DAY)
        self.assertEqual(len(frame),60)
        self.assertEqual(frame.bid.iloc[0],1.1)
        self.assertEqual(frame.ask.iloc[0],1.1001)
        raw=lzma.compress(struct.pack('>IIIff',2,2000100,2000000,1.,2.)+struct.pack('>IIIff',1,2000100,2000000,1.,2.))
        with self.assertRaisesRegex(ValueError,'orden'):
            decode_hour(raw,'XAUUSD',DAY)
        with self.assertRaises(ValueError): decode_hour(b'broken','EURUSD',DAY)

    def test_download_is_resumable_and_second_call_has_no_network(self):
        calls=[]
        def fetch(url):
            calls.append(url)
            return bi5()
        download_history(self.root,['EURUSD'],DAY,DAY+pd.Timedelta(days=1),fetch=fetch)
        self.assertEqual(len(calls),24)
        download_history(self.root,['EURUSD'],DAY,DAY+pd.Timedelta(days=1),fetch=lambda u:self.fail('redescarga'))
        frame,_=HistoryStore(self.root).read('EURUSD',DAY,DAY+pd.Timedelta(days=1))
        self.assertEqual(len(frame),1440)

    def test_failed_download_does_not_claim_complete(self):
        from urllib.error import HTTPError
        def fetch(url): raise HTTPError(url,429,'rate limited',{},None)
        with self.assertRaises(HistoryError):
            download_history(self.root,['EURUSD'],DAY,DAY+pd.Timedelta(days=1),fetch=fetch)
        report=json.loads((self.root/'download_status.json').read_text())
        self.assertEqual(report['status'],'RATE_LIMITED')
        self.assertEqual(report['completed_days'],0)

    def test_local_contract_and_tick_loader_use_catalog(self):
        from tick_data import load_tick_manifest
        from gherkin_spec import parse_markdown
        from test_tick_pipeline import TEXT
        contract=parse_markdown(TEXT)['contract']
        contract.update(source='Dukascopy',contract='Dukascopy EURUSD')
        contract['evaluation'].update(start=str(DAY),end=str(DAY+pd.Timedelta(days=1)))
        HistoryStore(self.root).put_day('EURUSD',DAY,quotes(),{},[])
        with patch('socket.socket.connect',side_effect=AssertionError('offline')):
            data,meta=load_tick_manifest(self.root/'catalog.duckdb',contract)
        self.assertEqual(len(data),1440)
        self.assertEqual(meta['source'],'Dukascopy')
        contract['contract']='otro broker'
        with self.assertRaisesRegex(HistoryError,'contract'):
            load_local_contract(contract,self.root)

    def test_public_evaluation_does_not_download_missing_cache(self):
        from market_data import load_public_data
        with patch('market_data.download_asset',side_effect=AssertionError('download during evaluation')):
            with self.assertRaisesRegex(ValueError,'local'):
                load_public_data(self.root,start='2024-01-01',end='2024-01-10')


if __name__=='__main__': unittest.main()
