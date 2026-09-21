"""Velas públicas diarias; no requiere archivos ni credenciales de mercado.

JSON Dukascopy para FX/oro y Coinbase Exchange para BTC-USD spot.
Los snapshots se congelan al inicio y se conservan con hash y procedencia.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

ASSETS = {
    "EURUSD": {"label": "EUR/USD", "symbol": "EUR-USD", "market": "Forex"},
    "XAUUSD": {"label": "XAU/USD · Oro", "symbol": "XAU-USD", "market": "Oro"},
    "GBPUSD": {"label": "GBP/USD", "symbol": "GBP-USD", "market": "Forex"},
    "CADUSD": {"label": "CAD/USD", "symbol": "USD-CAD", "market": "Forex", "inverse": True},
    "BTCUSD": {"label": "BTC/USD", "symbol": "BTC-USD", "market": "Cripto spot"},
}


def request_json(url, checkpoint=lambda: None):
    for attempt in range(3):
        checkpoint()
        try:
            with urlopen(Request(url, headers={"User-Agent": "QuantLab/1.0", "Accept": "application/json"}), timeout=10) as response:
                raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ValueError("Respuesta de mercado demasiado grande")
            result = json.loads(raw)
            checkpoint()
            return result
        except (OSError, ValueError) as exc:
            if attempt == 2:
                raise RuntimeError(f"No se pudo descargar {url}: {exc}") from exc
            for _ in range(5 * (attempt + 1)):
                checkpoint()
                time.sleep(.1)


def decode_duka(payload):
    """Reconstruye columnas de deltas. No inventa velas para los huecos."""
    scale, shift = float(payload["multiplier"]), int(payload["shift"])
    delta = np.asarray(payload["times"], dtype=float)
    if scale <= 0 or not np.isfinite(scale) or shift != 86400000 or not np.isfinite(delta).all() or (delta < 0).any():
        raise ValueError("Esquema diario Dukascopy inválido")
    columns = {"timestamp": pd.to_datetime(payload["timestamp"] + np.cumsum(delta) * shift, unit="ms", utc=True)}
    for field in ("open", "high", "low", "close"):
        changes = np.asarray(payload[field + "s"], dtype=float)
        if len(changes) != len(delta):
            raise ValueError("Columnas Dukascopy de diferente longitud")
        columns[field] = (round(payload[field] / scale) + np.cumsum(changes)) * scale
    return pd.DataFrame(columns)


def invert_ohlc(frame):
    result = frame.copy()
    result["open"], result["close"] = 1 / frame.open, 1 / frame.close
    result["high"], result["low"] = 1 / frame.low, 1 / frame.high
    return result


def download_asset(asset, start, end, checkpoint=lambda: None, fetch=request_json):
    """Rango UTC [start,end); end nunca incluye el día actual incompleto."""
    spec = ASSETS[asset]
    chunks, urls = [], []
    if asset == "BTCUSD":
        cursor = start
        while cursor < end:
            checkpoint()
            boundary = min(cursor + pd.Timedelta(days=299), end)
            url = "https://api.exchange.coinbase.com/products/BTC-USD/candles?" + urlencode({
                "granularity": 86400, "start": cursor.isoformat(), "end": boundary.isoformat()})
            payload = fetch(url, checkpoint)
            part = pd.DataFrame(payload, columns=["timestamp", "low", "high", "open", "close", "volume"])
            part["timestamp"] = pd.to_datetime(part.timestamp, unit="s", utc=True)
            chunks.append(part[(part.timestamp >= cursor) & (part.timestamp < boundary)])
            urls.append(url)
            cursor = boundary
    else:
        for year in range(start.year, end.year + 1):
            checkpoint()
            base = f"https://jetta.dukascopy.com/v1/candles/day/{spec['symbol']}/BID"
            url = (base + "?from=" + str(int(pd.Timestamp(f"{year}-01-01", tz="UTC").timestamp()*1000))
                   if year == pd.Timestamp.now(tz="UTC").year else f"{base}/{year}")
            chunks.append(decode_duka(fetch(url, checkpoint)))
            urls.append(url)
    frame = pd.concat(chunks, ignore_index=True).sort_values("timestamp")
    frame = frame[(frame.timestamp >= start) & (frame.timestamp < end)].copy()
    if frame.timestamp.duplicated().any():
        raise ValueError(f"{asset}: velas duplicadas del proveedor")
    if asset != "BTCUSD":
        # Conserva sesiones cortas del domingo dentro de la barra del lunes.
        sunday = frame.timestamp.dt.weekday == 6
        frame.loc[sunday, "timestamp"] += pd.Timedelta(days=1)
        frame = frame.groupby("timestamp", as_index=False).agg(open=("open", "first"), high=("high", "max"),
                                                             low=("low", "min"), close=("close", "last"))
        frame = frame[frame.timestamp < end]
        if spec.get("inverse"):
            frame = invert_ohlc(frame)
    return frame.reset_index(drop=True), urls


def load_public_data(cache: Path, checkpoint=lambda: None, progress=lambda message: None,
                     start="2018-01-01", end=None):
    from trading_agent import validate_data
    start = pd.Timestamp(start, tz="UTC")
    today = pd.Timestamp.now(tz="UTC").normalize()
    end = min(pd.Timestamp(end, tz="UTC") if end else today, today)
    cache.mkdir(parents=True, exist_ok=True)
    datasets, metadata = {}, {}
    for asset, spec in ASSETS.items():
        checkpoint()
        progress(f"Descargando {spec['label']} · velas diarias públicas")
        file = cache / f"{asset}_{start.date()}_{end.date()}_v2.json"
        if file.exists():
            snapshot = json.loads(file.read_text(encoding="utf-8"))
            frame, urls = pd.DataFrame(snapshot["candles"]), snapshot["urls"]
        else:
            frame, urls = download_asset(asset, start, end, checkpoint)
        frame = validate_data(frame)
        if (end - frame.timestamp.iloc[-1]).days > 7:
            raise ValueError(f"{asset}: fuente desactualizada, última barra {frame.timestamp.iloc[-1]}")
        if (frame.timestamp.iloc[0] - start).days > 7 or frame.timestamp.diff().dt.days.max() > 7:
            raise ValueError(f"{asset}: cobertura histórica incompleta; no se rellenan precios")
        candles = json.loads(frame.to_json(orient="records", date_format="iso"))
        digest = hashlib.sha256(json.dumps(candles, sort_keys=True).encode()).hexdigest()
        if not file.exists():
            file.write_text(json.dumps({"candles": candles, "urls": urls}), encoding="utf-8")
        datasets[asset] = frame
        metadata[asset] = {**spec, "source": "Coinbase Exchange" if asset == "BTCUSD" else "Dukascopy",
            "quote": "trades" if asset == "BTCUSD" else "inverse BID (ASK)" if spec.get("inverse") else "BID",
            "bars": len(frame), "start": str(frame.timestamp.iloc[0]), "end": str(frame.timestamp.iloc[-1]),
            "sha256": digest, "urls": urls, "snapshot_file": str(file.resolve()), "timeframe": "1d"}
    # Mismo rango de calendario para comparar; se mantienen fines de semana de BTC.
    common_start = max(f.timestamp.iloc[0] for f in datasets.values())
    common_end = min(f.timestamp.iloc[-1] for f in datasets.values())
    for asset, frame in datasets.items():
        datasets[asset] = validate_data(frame[(frame.timestamp >= common_start) & (frame.timestamp <= common_end)])
        metadata[asset].update(comparison_start=str(common_start), comparison_end=str(common_end))
    return datasets, metadata
