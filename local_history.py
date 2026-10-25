"""Immutable local Parquet history and DuckDB catalogue. This module has no downloader."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import uuid

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from tick_data import utc, validate_ticks


DEFAULT_ROOT = Path(__file__).resolve().parent / 'data'
INSTRUMENTS = {
    'EURUSD': {'scale':100000, 'instrument_type':'fx', 'quote_currency':'USD'},
    'GBPUSD': {'scale':100000, 'instrument_type':'fx', 'quote_currency':'USD'},
    'USDJPY': {'scale':1000, 'instrument_type':'fx', 'quote_currency':'JPY'},
    'USDCAD': {'scale':100000, 'instrument_type':'fx', 'quote_currency':'CAD'},
    'XAUUSD': {'scale':1000, 'instrument_type':'cfd', 'quote_currency':'USD'},
}
STORAGE = 'parquet_duckdb_v1'
QUALITY_VERSION = 'quotes-v1'


class HistoryError(ValueError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024),b''): h.update(block)
    return h.hexdigest()


def atomic_json(path, value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def days(start,end):
    start,end=utc(start),utc(end)
    if start>=end: raise HistoryError('INVALID_DATA','Intervalo vacío')
    return pd.date_range(start.floor('D'),(end-pd.Timedelta(nanoseconds=1)).floor('D'),freq='D')


def quality(frame, issues):
    issues=list(issues)
    metrics={'ticks':len(frame),'version':QUALITY_VERSION}
    if len(frame)<2:
        issues.append('fewer_than_two_ticks')
    else:
        frame=validate_ticks(frame)
        mid=(frame.bid+frame.ask)/2
        spreads=(frame.ask-frame.bid)/mid*10000
        gaps=frame.timestamp.diff().dt.total_seconds().dropna()
        jumps=mid.pct_change().abs().dropna()
        metrics.update(spread_bps_max=float(spreads.max()), spread_bps_p99=float(spreads.quantile(.99)),
                       gap_seconds_max=float(gaps.max()),price_jump_max=float(jumps.max()),
                       repeated_quotes=int(frame.duplicated(['timestamp','bid','ask']).sum()))
        if spreads.max()>50: issues.append('spread_above_50_bps')
        # Session breaks are reported too; explicit evidence is required to clear a flag.
        if gaps.max()>300: issues.append('gap_above_300_seconds')
        if jumps.max()>.02: issues.append('jump_above_2_percent')
    return {**metrics,'status':'NEEDS_REVIEW' if issues else 'PASSED', 'issues':sorted(set(issues)),
            'execution_verified':False}


class HistoryStore:
    def __init__(self,root=DEFAULT_ROOT):
        root=Path(root).resolve()
        self.root=root.parent if root.suffix=='.duckdb' else root
        self.catalog=root if root.suffix=='.duckdb' else root/'catalog.duckdb'

    @contextmanager
    def connection(self,write=False):
        if not write and not self.catalog.is_file():
            raise HistoryError('NEEDS_DATA','Falta el catálogo local; ejecuta la descarga inicial')
        if write: self.root.mkdir(parents=True,exist_ok=True)
        con=duckdb.connect(str(self.catalog),read_only=not write,
                           config={'autoinstall_known_extensions':'false','autoload_known_extensions':'false'})
        try:
            if write:
                con.execute('''CREATE TABLE IF NOT EXISTS partitions (
                    asset VARCHAR, day VARCHAR, record VARCHAR, PRIMARY KEY(asset, day))''')
            yield con
        finally: con.close()

    def record(self,asset,day):
        if not self.catalog.exists(): return None
        with self.connection() as con:
            row=con.execute('SELECT record FROM partitions WHERE asset=? AND day=?',[asset,str(utc(day).date())]).fetchone()
        return json.loads(row[0]) if row else None

    def put_day(self,asset,day,frame,provenance,issues,replace=False):
        if asset not in INSTRUMENTS: raise HistoryError('NEEDS_CAPABILITY','Instrumento no disponible: '+asset)
        day=utc(day)
        if day!=day.floor('D'): raise HistoryError('INVALID_DATA','Partición requiere inicio UTC del día')
        if day+pd.Timedelta(days=1)>pd.Timestamp.now(tz='UTC'):
            raise HistoryError('INVALID_DATA','No se guardan días incompletos o futuros')
        if self.record(asset,day) and not replace: raise HistoryError('DATA_EXISTS','Partición ya existe')
        frame=frame.copy()
        if len(frame)>=2: frame=validate_ticks(frame)
        if len(frame):
            frame['timestamp']=pd.to_datetime(frame.timestamp,utc=True)
            if (frame.timestamp<day).any() or (frame.timestamp>=day+pd.Timedelta(days=1)).any():
                raise HistoryError('INVALID_DATA','Ticks fuera de su partición')
        report=quality(frame,issues)
        if frame.empty and day.weekday()==5 and not issues and len(provenance.get('hours',[]))==24 and all(
                h.get('status')=='CLOSED_WEEKEND' for h in provenance['hours']):
            report.update(status='CLOSED_WEEKEND',issues=[])
        relative=Path('dukascopy')/asset/str(day.year)/f'{day:%m-%d}-{uuid.uuid4().hex}.parquet'
        path=self.root/relative
        path.parent.mkdir(parents=True,exist_ok=True)
        schema=pa.schema([('timestamp',pa.timestamp('ns',tz='UTC')),('sequence',pa.int64()),('bid',pa.float64()),('ask',pa.float64())])
        pq.write_table(pa.Table.from_pandas(frame,schema=schema,preserve_index=False),path,compression='zstd')
        record={'asset':asset,'day':str(day.date()),'path':relative.as_posix(),'sha256':sha256(path),
                'source':'Dukascopy','contract':'Dukascopy '+asset,
                'instrument_type':INSTRUMENTS[asset]['instrument_type'],'quote_currency':INSTRUMENTS[asset]['quote_currency'],
                'quality':report,'provenance':provenance,'storage':STORAGE}
        atomic_json(path.with_suffix('.quality.json'),record)
        with self.connection(write=True) as con:
            con.execute('INSERT OR REPLACE INTO partitions VALUES (?,?,?)',[asset,str(day.date()),json.dumps(record)])
        return record

    def checked_path(self,record):
        path=(self.root/record['path']).resolve()
        if not path.is_relative_to(self.root) or path.suffix!='.parquet':
            raise HistoryError('INVALID_DATA','Ruta de partición inválida')
        if not path.is_file() or sha256(path)!=record['sha256']:
            raise HistoryError('DATA_CHANGED','Archivo local ausente o hash alterado: '+str(path))
        return path

    def records(self,asset,start,end):
        wanted=[str(d.date()) for d in days(start,end)]
        with self.connection() as con:
            rows=con.execute('SELECT record FROM partitions WHERE asset=? AND day>=? AND day<=? ORDER BY day',
                             [asset,wanted[0],wanted[-1]]).fetchall()
        result=[json.loads(row[0]) for row in rows]
        absent=sorted(set(wanted)-{r['day'] for r in result})
        if absent: raise HistoryError('NEEDS_DATA',f'Falta histórico local {asset}: {len(absent)} días; primero {absent[0]}. No se descargará durante la evaluación.')
        return result

    def read(self,asset,start,end):
        start,end=utc(start),utc(end)
        records=self.records(asset,start,end)
        bad=[r['day'] for r in records if r['quality']['status'] not in ('PASSED','CLOSED_WEEKEND')]
        if bad: raise HistoryError('DATA_QUALITY_FAILED',f'Histórico bloqueado por calidad: {asset}, {len(bad)} días; primero {bad[0]}')
        paths=[str(self.checked_path(r)) for r in records]
        with duckdb.connect(config={'autoinstall_known_extensions':'false','autoload_known_extensions':'false'}) as con:
            frame=con.execute('SELECT timestamp,sequence,bid,ask FROM read_parquet(?) WHERE timestamp>=? AND timestamp<? ORDER BY timestamp,sequence',
                              [paths,start.to_pydatetime(),end.to_pydatetime()]).fetchdf()
        frame=validate_ticks(frame)
        provenance=[{'day':r['day'],'file':r['path'],'sha256':r['sha256'],'quality':r['quality']} for r in records]
        digest=hashlib.sha256(json.dumps(provenance,sort_keys=True).encode()).hexdigest()
        return frame,{'storage':STORAGE,'source':'Dukascopy','contract':'Dukascopy '+asset,
                      'asset':asset,'timeframe':'tick','quote_currency':INSTRUMENTS[asset]['quote_currency'],
                      'instrument_type':INSTRUMENTS[asset]['instrument_type'], 'sha256':digest,'partitions':provenance,
                      'coverage_start':str(start),'coverage_end':str(end),'ticks':len(frame),
                      'catalog':str(self.catalog),'execution_verified':False}

    def status(self):
        if not self.catalog.exists(): return {'storage':STORAGE,'catalog':str(self.catalog),'assets':[]}
        with self.connection() as con:
            records=[json.loads(r[0]) for r in con.execute('SELECT record FROM partitions').fetchall()]
        assets=[]
        for asset in sorted({r['asset'] for r in records}):
            rows=[r for r in records if r['asset']==asset]
            assets.append({'asset':asset,'days':len(rows),'first_day':min(r['day'] for r in rows),
                           'last_day':max(r['day'] for r in rows),'ticks':sum(r['quality']['ticks'] for r in rows),
                           'needs_review':sum(r['quality']['status']=='NEEDS_REVIEW' for r in rows)})
        return {'storage':STORAGE,'catalog':str(self.catalog),'assets':assets}


def load_local_contract(contract,root=DEFAULT_ROOT):
    asset=contract['asset']
    if asset not in INSTRUMENTS: raise HistoryError('NEEDS_CAPABILITY','No hay adaptador Dukascopy para '+asset)
    expected={'source':'Dukascopy','contract':'Dukascopy '+asset,
              **{k:INSTRUMENTS[asset][k] for k in ('instrument_type','quote_currency')}}
    for name,value in expected.items():
        if contract[name]!=value:
            raise HistoryError('CONTRACT_MISMATCH',f'Catálogo local requiere {name}={value}; no se cambia el Gherkin')
    frame,meta=HistoryStore(root).read(asset,contract['evaluation']['start'],contract['evaluation']['end'])
    return frame,{**meta,'settings':contract['costs']}


def local_selection(path):
    return not path or Path(path).suffix=='.duckdb' or Path(path).is_dir()


def load_local_bars(contract,root=DEFAULT_ROOT):
    from execution import TIMEFRAMES
    ticks,meta=load_local_contract(contract,root)
    seconds=TIMEFRAMES.get(contract['data_timeframe'])
    side=contract['costs']['quote_side']
    if not seconds or side not in (0,2):
        raise HistoryError('NEEDS_CAPABILITY','Velas locales requieren temporalidad admitida y quote_side 0 (bid) o 2 (ask)')
    price='bid' if side==0 else 'ask'
    bars=ticks.set_index('timestamp')[price].resample(f'{seconds}s',origin='epoch',closed='left',label='left').ohlc().dropna().reset_index()
    start,end=utc(contract['evaluation']['start']),utc(contract['evaluation']['end'])
    bars=bars[(bars.timestamp>=start)&(bars.timestamp+pd.Timedelta(seconds=seconds)<=end)].reset_index(drop=True)
    return bars,{**meta,'timeframe':contract['data_timeframe'],'aggregation':price+' OHLC from ticks'}
