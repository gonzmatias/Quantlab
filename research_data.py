"""Causal research catalogue: prices, volume, calendar and timestamped evidence."""
import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field


class DataAcquisition(BaseModel):
    """A public point-in-time CSV, discovered in the current web investigation."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    source_url: str
    value_column: str
    availability_column: str
    availability_description: str = Field(min_length=40)
    max_age_days: int = Field(gt=0)


def fetch_public_csv(url):
    """Read bounded public HTTPS data, checking destinations and redirects."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit
    from urllib.request import HTTPRedirectHandler, Request, build_opener

    def check(target):
        parsed = urlsplit(target)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError("Los datos remotos requieren HTTPS público sin credenciales")
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError("La fuente debe ser pública; destinos locales o privados no admitidos")

    class CheckedRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            check(newurl)
            return super().redirect_request(request, fp, code, msg, headers, newurl)

    check(url)
    with build_opener(CheckedRedirect()).open(Request(url, headers={"User-Agent": "QuantLab/1.0"}), timeout=10) as response:
        body = response.read(8_000_001)
    if len(body) > 8_000_000:
        raise ValueError("La fuente supera el tamaño permitido por descarga")
    return body


def acquire_evidence(requests, known_urls, folder, checkpoint=lambda: None, fetch=fetch_public_csv):
    """Never infer publication dates from economic observation dates."""
    import io
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for request in requests:
        checkpoint()
        spec = DataAcquisition.model_validate(request).model_dump()
        if spec["source_url"] not in known_urls:
            raise ValueError("La URL de descarga no apareció en las fuentes de investigación")
        try:
            raw = fetch(spec["source_url"])
            frame = pd.read_csv(io.BytesIO(raw))
            times = pd.to_datetime(frame[spec["availability_column"]], utc=True, errors="raise")
            values = pd.to_numeric(frame[spec["value_column"]], errors="raise")
            if frame.empty or times.isna().any() or times.duplicated().any() or not np.isfinite(values).all():
                raise ValueError("Fechas de disponibilidad o valores inválidos")
        except (OSError, KeyError, ValueError) as exc:
            raise ValueError("No se pudo incorporar la fuente " + spec["name"] + ": " + str(exc)) from exc
        document = {k: spec[k] for k in ("name", "source_url", "availability_description", "max_age_days")}
        document["observations"] = [{"available_at": t.isoformat(), "value": float(v)} for t, v in zip(times, values)]
        document["download_sha256"] = hashlib.sha256(raw).hexdigest()
        document["acquisition"] = spec
        destination = folder / (spec["name"] + ".json")
        payload = json.dumps(document, allow_nan=False)
        if destination.exists() and destination.read_text(encoding="utf-8") != payload:
            raise ValueError("La fuente ya está congelada en esta ejecución; no se sobrescribirá " + spec["name"])
        destination.write_text(payload, encoding="utf-8")
        checkpoint()
    return load_evidence_series(folder)


def load_evidence_series(folder):
    """Local extensible data contract, including macro, on-chain and event series.

    available_at must be the actual time the value became known (including revisions),
    never merely the economic period it describes. No forward/back filling from future.
    """
    result, provenance = {}, {}
    for path in sorted(Path(folder).glob("*.json")):
        raw = path.read_bytes()
        document = json.loads(raw)
        name = document["name"]
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or name in result:
            raise ValueError("Nombre de serie externa inválido o repetido")
        if not document.get("source_url", "").startswith("https://") or not document.get("availability_description"):
            raise ValueError("La serie externa requiere fuente y explicación de disponibilidad histórica")
        age = document.get("max_age_days")
        if type(age) is not int or age <= 0:
            raise ValueError("La serie externa requiere max_age_days positivo")
        frame = pd.DataFrame(document["observations"])[["available_at", "value"]]
        frame["available_at"] = pd.to_datetime(frame.available_at, utc=True, errors="raise")
        frame["value"] = pd.to_numeric(frame.value, errors="raise")
        if frame.empty or frame.available_at.isna().any() or frame.available_at.duplicated().any() or not np.isfinite(frame.value).all():
            raise ValueError("Observaciones externas vacías, duplicadas o no finitas")
        result[name] = (frame.sort_values("available_at"), age)
        provenance[name] = {k: document[k] for k in ("name", "source_url", "availability_description", "max_age_days")}
        provenance[name].update(sha256=hashlib.sha256(raw).hexdigest(), file=str(path.resolve()))
    return result, provenance


def enrich_datasets(datasets, evidence=None):
    """Use external information strictly available before the target daily bar.

    A daily source candle is conservatively available at timestamp + 1 day.
    Source columns remain on the target calendar, with at most 7 days staleness.
    """
    enriched = {}
    for asset, original in datasets.items():
        frame = original.copy()
        frame["weekday"] = frame.timestamp.dt.weekday.astype(float)
        frame["month"] = frame.timestamp.dt.month.astype(float)
        frame["day"] = frame.timestamp.dt.day.astype(float)
        for source, other in datasets.items():
            if not re.fullmatch(r"[A-Za-z0-9_]+", source):
                raise ValueError("Identificador de activo inválido")
            columns = [c for c in ("open", "high", "low", "close", "volume") if c in other]
            right = other[["timestamp", *columns]].copy()
            right["timestamp"] += pd.Timedelta(days=1)
            right = right.rename(columns={c: f"market_{source.lower()}_{c}" for c in columns})
            frame = pd.merge_asof(frame, right, on="timestamp", direction="backward", tolerance=pd.Timedelta(days=7))
        for name, (observations, age) in (evidence or {}).items():
            right = observations.rename(columns={"available_at": "timestamp", "value": "external_" + name})
            frame = pd.merge_asof(frame, right, on="timestamp", direction="backward",
                                  allow_exact_matches=False, tolerance=pd.Timedelta(days=age))
        enriched[asset] = frame
    return enriched


def data_catalogue(training):
    """Training-only descriptive coverage, not a peek into future observations."""
    catalogue = {}
    for name in training.columns:
        if name == "timestamp":
            continue
        values = training[name].replace([np.inf, -np.inf], np.nan).dropna()
        catalogue[name] = {"observations": len(values), "coverage": len(values) / len(training),
                           "mean": float(values.mean()) if len(values) else None,
                           "std": float(values.std()) if len(values) > 1 else None}
    return catalogue


def training_diagnostics(training):
    """Exploratory associations only, using next-open returns entirely inside IS.

    These comparisons guide questions, never establish statistical significance.
    No p-values or claimed independent evidence are manufactured from this screen.
    """
    close = training.close
    features = {
        "return_1": close.pct_change(fill_method=None),
        "return_5": close.pct_change(5, fill_method=None),
        "return_20": close.pct_change(20, fill_method=None),
        "intraday_range": (training.high - training.low) / close,
        "opening_gap": training.open / close.shift(1) - 1,
        "volatility_20": close.pct_change(fill_method=None).rolling(20).std(),
    }
    for name in training.columns:
        if name == "volume":
            features["relative_volume_20"] = training[name] / training[name].rolling(20).mean().replace(0, np.nan)
        elif name.startswith("market_") and name.endswith("_close"):
            features[name + "_return_5"] = training[name].pct_change(5, fill_method=None)
        elif name.startswith("external_"):
            features[name] = training[name]
    associations, calendar = [], []
    for horizon in (1, 5, 20):
        # Entry at next open; target exit at close horizon bars later, all inside IS.
        forward = close.shift(-horizon) / training.open.shift(-1) - 1
        for name, values in features.items():
            pairs = pd.DataFrame({"feature": values, "return": forward}).replace([np.inf, -np.inf], np.nan).dropna()
            if len(pairs) < 60 or pairs.feature.nunique() < 2 or pairs["return"].nunique() < 2:
                continue
            correlation = float(pairs.feature.corr(pairs["return"]))
            if not np.isfinite(correlation):
                continue
            low, high = pairs.feature.quantile([.25, .75])
            associations.append({"feature": name, "horizon": horizon, "observations": len(pairs),
                                 "correlation": correlation,
                                 "low_quartile_gross_return": float(pairs.loc[pairs.feature <= low, "return"].mean()),
                                 "high_quartile_gross_return": float(pairs.loc[pairs.feature >= high, "return"].mean())})
        for weekday, group in forward.groupby(training.timestamp.dt.weekday):
            values = group.dropna()
            if len(values):
                calendar.append({"weekday": int(weekday), "horizon": horizon,
                                 "observations": len(values), "gross_return": float(values.mean())})
    return {"warning": "Exploración IS, retornos brutos, ventanas solapadas; asociaciones seleccionadas no prueban ventaja ni significancia.",
            "comparisons_computed": len(associations) + len(calendar),
            "associations": sorted(associations, key=lambda row: abs(row["correlation"]), reverse=True),
            "calendar": calendar}
