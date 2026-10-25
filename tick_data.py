"""Explicit tick contract and local quote data. No synthetic ticks or price filling."""
from pathlib import Path
import hashlib
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator


TICK_MODEL = "tick_bid_ask_v1"
RETURN_SECONDS = {"1h": 3600, "4h": 14400, "1d": 86400}


class TickError(ValueError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class TickExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    signal_price: Literal["bid", "ask", "mid"]
    distance_unit: Literal["percent", "price", "pips"]
    pip_size: float | None = Field(gt=0)
    entry_order: Literal["market", "limit"]
    entry_limit_offset: float = Field(ge=0)
    order_ttl_ms: int = Field(strict=True, gt=0)
    target_order: Literal["market", "limit"]
    latency_ms: int = Field(strict=True, ge=0)
    holding_unit: Literal["milliseconds", "signal_events"]
    max_quote_gap_ms: int = Field(strict=True, gt=0)
    return_interval: Literal["1h", "4h", "1d"]
    liquidate_at_end: Literal[True]

    @model_validator(mode="after")
    def consistent(self):
        if (self.distance_unit == "pips") != (self.pip_size is not None):
            raise ValueError("pip_size es obligatorio solo con distance_unit=pips")
        if self.entry_order == "market" and self.entry_limit_offset != 0:
            raise ValueError("Entrada market requiere entry_limit_offset=0")
        if self.order_ttl_ms <= self.latency_ms:
            raise ValueError("order_ttl_ms debe superar latency_ms")
        return self


def utc(value):
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError("Timestamp requiere zona horaria explícita")
    return result.tz_convert("UTC")


def validate_ticks(frame):
    required = {"timestamp", "sequence", "bid", "ask"}
    if set(frame.columns) != required or len(frame) < 2:
        raise TickError("INVALID_DATA", "Ticks requieren exactamente timestamp,sequence,bid,ask y al menos 2 filas")
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime([utc(t) for t in frame.timestamp], utc=True)
    seq = frame.sequence
    if (not pd.api.types.is_integer_dtype(seq.dtype) or pd.api.types.is_bool_dtype(seq.dtype)
            or seq.isna().any() or (seq < 0).any()):
        raise TickError("INVALID_DATA", "sequence requiere enteros no negativos")
    delta = frame.timestamp.diff()
    if (delta.dropna() < pd.Timedelta(0)).any() or ((delta == pd.Timedelta(0)) & (seq.diff() <= 0)).any():
        raise TickError("INVALID_DATA", "Ticks fuera de orden o repetidos; timestamp/sequence debe crecer")
    for name in ("bid", "ask"):
        if pd.api.types.is_bool_dtype(frame[name]):
            raise TickError("INVALID_DATA", "Precio booleano")
        frame[name] = pd.to_numeric(frame[name], errors="raise").astype(float)
    values = frame[["bid", "ask"]].to_numpy()
    if not np.isfinite(values).all() or (values <= 0).any() or (frame.bid > frame.ask).any():
        raise TickError("INVALID_DATA", "Cotizaciones no finitas, no positivas o bid > ask")
    return frame.reset_index(drop=True)


def load_tick_manifest(path, contract):
    # Local import avoids a parser/engine import cycle.
    from gherkin_spec import json_value
    from local_history import local_selection, load_local_contract, DEFAULT_ROOT
    if local_selection(path):
        return load_local_contract(contract,path or DEFAULT_ROOT)
    path = Path(path).resolve()
    manifest = json_value(path.read_text("utf-8-sig"))
    if not isinstance(manifest, dict) or set(manifest) != {"datasets"} or not isinstance(manifest["datasets"], list):
        raise TickError("INVALID_DATA", "Manifiesto requiere datasets")
    selected = [s for s in manifest["datasets"] if s.get("asset") == contract["asset"]]
    if len(selected) != 1:
        raise TickError("NEEDS_DATA", "Se requiere un único dataset para " + contract["asset"])
    spec = selected[0]
    required = {"asset", "source", "contract", "instrument_type", "quote_currency", "timeframe",
                "file", "settings", "coverage_start", "coverage_end"}
    if set(spec) != required:
        raise TickError("INVALID_DATA", "Campos de manifiesto tick incompletos o desconocidos")
    for field in ("asset", "source", "contract", "instrument_type", "quote_currency"):
        if spec[field] != contract[field]:
            raise TickError("CONTRACT_MISMATCH", "Manifiesto distinto del Gherkin: " + field)
    if spec["timeframe"] != "tick" or spec["settings"] != contract["costs"]:
        raise TickError("CONTRACT_MISMATCH", "Formato o costos diferentes del documento")
    start, end = utc(spec["coverage_start"]), utc(spec["coverage_end"])
    requested_start, requested_end = utc(contract["evaluation"]["start"]), utc(contract["evaluation"]["end"])
    if start > requested_start or end < requested_end or start >= end:
        raise TickError("INSUFFICIENT_DATA", "Cobertura declarada no abarca el período solicitado")
    source = (path.parent / spec["file"]).resolve()
    raw = source.read_bytes()
    import io
    frame = validate_ticks(pd.read_csv(io.BytesIO(raw)))
    if frame.timestamp.iloc[0] < start or frame.timestamp.iloc[-1] >= end:
        raise TickError("INVALID_DATA", "Ticks fuera de la cobertura declarada")
    if frame.timestamp.iloc[-1] > pd.Timestamp.now(tz="UTC") or requested_end > pd.Timestamp.now(tz="UTC"):
        raise TickError("INVALID_DATA", "Datos o período futuros")
    return frame, {**spec, "sha256": hashlib.sha256(raw).hexdigest(), "ticks": len(frame),
                   "snapshot_file": str(source), "execution_verified": False}
