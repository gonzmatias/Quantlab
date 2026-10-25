"""Explicit, resumable Dukascopy BI5 acquisition. Never called by an evaluator."""
from contextlib import contextmanager
import hashlib
import json
import lzma
from pathlib import Path
import shutil
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

import numpy as np
import pandas as pd

from local_history import HistoryError, HistoryStore, INSTRUMENTS, atomic_json, days, sha256
from tick_data import utc, validate_ticks


def empty_ticks():
    return pd.DataFrame({'timestamp':pd.Series([],dtype='datetime64[ns, UTC]'),
                         'sequence':pd.Series([],dtype='int64'),'bid':pd.Series([],dtype='float64'),
                         'ask':pd.Series([],dtype='float64')})


def decode_hour(raw,asset,hour):
    if asset not in INSTRUMENTS: raise HistoryError('NEEDS_CAPABILITY','Instrumento sin escala verificada: '+asset)
    hour=utc(hour)
    if hour!=hour.floor('h'): raise ValueError('Hora debe alinearse en UTC')
    if not raw: return empty_ticks()
    try:
        decoder=lzma.LZMADecompressor(memlimit=256*1024*1024)
        decoded=decoder.decompress(raw,max_length=128*1024*1024+1)
        if not decoder.eof or decoder.unused_data or len(decoded)>128*1024*1024 or len(decoded)%20:
            raise ValueError('BI5 incompleto, concatenado o demasiado grande')
    except lzma.LZMAError as exc:
        raise HistoryError('INVALID_DATA','BI5 no válido') from exc
    records=np.frombuffer(decoded,dtype=np.dtype([('ms','>u4'),('ask','>u4'),('bid','>u4'),('ask_volume','>f4'),('bid_volume','>f4')]))
    if not len(records): return empty_ticks()
    if (records['ms']>=3600000).any() or (np.diff(records['ms'].astype('int64'))<0).any():
        raise HistoryError('INVALID_DATA','Ticks fuera de hora u orden')
    if any(not np.isfinite(records[k]).all() or (records[k]<0).any() for k in ('ask_volume','bid_volume')):
        raise HistoryError('INVALID_DATA','Volumen BI5 inválido')
    frame=pd.DataFrame({'timestamp':hour+pd.to_timedelta(records['ms'].astype('int64'),unit='ms'),
                        'sequence':np.arange(len(records),dtype='int64'),
                        'bid':records['bid'].astype(float)/INSTRUMENTS[asset]['scale'],
                        'ask':records['ask'].astype(float)/INSTRUMENTS[asset]['scale']})
    if len(frame)>=2: return validate_ticks(frame)
    if frame.bid.iloc[0]<=0 or frame.ask.iloc[0]<frame.bid.iloc[0]: raise ValueError('Cotización BI5 inválida')
    return frame


def hour_url(asset,hour):
    return f'https://datafeed.dukascopy.com/datafeed/{asset}/{hour.year}/{hour.month-1:02d}/{hour.day:02d}/{hour.hour:02d}h_ticks.bi5'


def fetch_hour(url):
    with urlopen(Request(url,headers={'User-Agent':'QuantLab/1.0 historical research'}),timeout=30) as response:
        raw=response.read(32*1024*1024+1)
    if len(raw)>32*1024*1024: raise HistoryError('INVALID_DATA','Respuesta BI5 demasiado grande')
    return raw


def weekend_closed(hour):
    # Conservative subset, identical in summer/winter: no inferred holidays/maintenance.
    return hour.weekday()==5 or (hour.weekday()==6 and hour.hour<20) or (hour.weekday()==4 and hour.hour>=23)


@contextmanager
def download_lock(root):
    root.mkdir(parents=True,exist_ok=True)
    with (root/'download.lock').open('a+b') as stream:
        stream.seek(0)
        if not stream.read(1): stream.write(b'0'); stream.flush()
        stream.seek(0)
        import os
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            raise HistoryError('BUSY','Ya hay una descarga activa en esta biblioteca') from exc
        try: yield
        finally:
            stream.seek(0)
            if os.name=='nt': msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def download_history(root,assets,start,end,fetch=fetch_hour,checkpoint=lambda:None,progress=lambda r:None,retry_failed=False):
    root=Path(root).resolve()
    start,end=utc(start),utc(end)
    if start!=start.floor('D') or end!=end.floor('D') or end>pd.Timestamp.now(tz='UTC').floor('D'):
        raise HistoryError('INVALID_DATA','Descarga requiere días UTC completos, fin exclusivo no futuro')
    period=days(start,end)
    if not assets or len(set(assets))!=len(assets) or set(assets)-INSTRUMENTS.keys():
        raise HistoryError('NEEDS_CAPABILITY','Instrumentos exactos disponibles: '+', '.join(INSTRUMENTS))
    store=HistoryStore(root)
    report={'status':'DOWNLOADING','assets':list(assets),'start':str(start),'end':str(end),
            'planned_days':len(period)*len(assets),'completed_days':0,'review_days':0,'reused_days':0,
            'downloaded_hours':0,'source':'Dukascopy','root':str(root)}
    def save():
        report['updated_at']=str(pd.Timestamp.now(tz='UTC'))
        atomic_json(root/'download_status.json',report)
        progress(dict(report))
    with download_lock(root):
        save()
        try:
            for day in period:
                for asset in assets:
                    checkpoint()
                    report.update(current_asset=asset,current_day=str(day.date()))
                    existing=store.record(asset,day)
                    if existing and not (retry_failed and existing['quality']['status']=='NEEDS_REVIEW'):
                        store.checked_path(existing)
                        report['reused_days']+=1
                        report['review_days' if existing['quality']['status']=='NEEDS_REVIEW' else 'completed_days']+=1
                        save()
                        continue
                    if shutil.disk_usage(root).free<2*1024**3:
                        raise HistoryError('DISK_SPACE','Menos de 2 GiB libres; descarga detenida sin borrar datos')
                    parts,sources,issues=[],[],[]
                    for hour in pd.date_range(day,periods=24,freq='h'):
                        checkpoint()
                        if weekend_closed(hour):
                            sources.append({'hour':str(hour),'status':'CLOSED_WEEKEND'})
                            continue
                        url=hour_url(asset,hour)
                        raw_path=root/'raw'/'dukascopy'/asset/f'{hour:%Y/%m/%d/%H}.bi5'
                        receipt=raw_path.with_suffix('.json')
                        raw=None
                        if receipt.exists():
                            entry=json.loads(receipt.read_text('utf-8'))
                            if entry['status']=='FETCHED':
                                if not raw_path.is_file() or sha256(raw_path)!=entry['sha256']:
                                    raise HistoryError('DATA_CHANGED','Original BI5 con hash alterado: '+str(raw_path))
                                raw=raw_path.read_bytes()
                            elif not retry_failed:
                                issues.append('missing_hour:'+str(hour))
                                sources.append(entry)
                                continue
                        if raw is None:
                            try: raw=fetch(url)
                            except HTTPError as exc:
                                if exc.code==429:
                                    report['retry_after']=str(exc.headers.get('Retry-After','no indicado'))
                                    raise HistoryError('RATE_LIMITED','Dukascopy respondió HTTP 429; reanuda más tarde. Se conserva lo descargado.') from exc
                                if exc.code!=404: raise
                                entry={'hour':str(hour),'url':url,'status':'MISSING_HTTP_404'}
                                atomic_json(receipt,entry)
                                issues.append('missing_hour:'+str(hour))
                                sources.append(entry)
                                continue
                            raw_path.parent.mkdir(parents=True,exist_ok=True)
                            temp=raw_path.with_name(raw_path.name+'.'+uuid.uuid4().hex+'.tmp')
                            temp.write_bytes(raw)
                            temp.replace(raw_path)
                            entry={'hour':str(hour),'url':url,'status':'FETCHED','sha256':sha256(raw_path),
                                   'bytes':len(raw),'fetched_at':str(pd.Timestamp.now(tz='UTC'))}
                            atomic_json(receipt,entry)
                            report['downloaded_hours']+=1
                        sources.append(entry)
                        try:
                            part=decode_hour(raw,asset,hour)
                            if part.empty: issues.append('empty_hour:'+str(hour))
                            else: parts.append(part)
                        except (ValueError,OverflowError) as exc:
                            issues.append('invalid_hour:'+str(hour)+':'+str(exc))
                        report['current_hour']=str(hour)
                        save()
                    frame=pd.concat(parts,ignore_index=True) if parts else empty_ticks()
                    frame['sequence']=np.arange(len(frame),dtype='int64')
                    record=store.put_day(asset,day,frame,{'hours':sources,'scale':INSTRUMENTS[asset]['scale'],
                              'decoder':'dukascopy-hour-bi5-v1'},issues,replace=bool(existing))
                    report['review_days' if record['quality']['status']=='NEEDS_REVIEW' else 'completed_days']+=1
                    save()
            report['status']='NEEDS_REVIEW' if report['review_days'] else 'COMPLETE'
        except BaseException as exc:
            report.update(status=getattr(exc,'status','INTERRUPTED'),error=str(exc))
            raise
        finally: save()
    return report
