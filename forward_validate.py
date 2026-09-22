"""Evaluate a frozen candidate on prices observed after freezing, without an LLM.

Usage: python forward_validate.py outputs/<run>/candidate.json
The first interval meeting 120 signal bars and the frozen calendar horizon is tested once.
This is a forward price simulation, not a broker execution certification.
"""
import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from experiment_protocol import digest, evaluate_frozen, engine_hashes, confirmation_end
from market_data import load_public_data
from research_data import load_evidence_series, enrich_datasets
from trading_agent import Settings, validate_data


def evaluate_candidate(path, datasets, evidence=None, market_metadata=None):
    path = Path(path)
    frozen = json.loads(path.read_text(encoding="utf-8"))
    signature = frozen.pop("sha256")
    if digest(frozen) != signature:
        raise ValueError("El candidato fue modificado después de congelarse")
    original_evidence, _ = load_evidence_series(path.parent / "research_data")
    for name, expected in frozen.get("evidence_hashes", {}).items():
        raw = (path.parent / "research_data" / (name + ".json")).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("Snapshot auxiliar modificado: " + name)
    merged_evidence = {}
    for name in frozen.get("evidence_hashes", {}):
        original, age = original_evidence[name]
        if name in (evidence or {}):
            incoming, incoming_age = evidence[name]
            if age != incoming_age:
                raise ValueError("No se puede cambiar la caducidad de una serie congelada")
            incoming = incoming[incoming.available_at > pd.Timestamp(frozen["frozen_at"])]
            original = pd.concat([original, incoming], ignore_index=True).drop_duplicates("available_at", keep="first").sort_values("available_at")
        merged_evidence[name] = (original, age)
    result_path = path.parent / "forward_validation.json"
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result.get("candidate_sha256") != signature:
            raise ValueError("El resultado prospectivo pertenece a otro candidato")
        return result
    if frozen.get("engine_hashes") and frozen["engine_hashes"] != engine_hashes():
        raise ValueError("El motor cambió después de congelar el candidato; usar la revisión original para validación prospectiva")
    contract = frozen.get("instrument_metadata", {})
    if contract.get("explicit_contract"):
        incoming = (market_metadata or {}).get(frozen["asset"], {})
        fields = ("contract", "source", "quote_currency", "instrument_type", "timeframe", "settings")
        if any(contract.get(k) != incoming.get(k) for k in fields):
            raise ValueError("El contrato, fuente o costos prospectivos no coinciden con el manifiesto congelado")
    combined = {}
    for asset, expected in frozen["snapshot_hashes"].items():
        raw = (path.parent / "candidate_data" / (asset + ".json")).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("Snapshot histórico modificado: " + asset)
        old = validate_data(pd.DataFrame(json.loads(raw)))
        new = validate_data(datasets[asset])
        from execution import bar_seconds
        if bar_seconds(old) != bar_seconds(new):
            raise ValueError("La resolución prospectiva difiere de la congelada")
        combined[asset] = pd.concat([old, new[new.timestamp > old.timestamp.iloc[-1]]], ignore_index=True)
    data = enrich_datasets(combined, merged_evidence)[frozen["asset"]]
    after = max(pd.Timestamp(frozen["observed_through"]), pd.Timestamp(frozen["frozen_at"]))
    start = int(data.timestamp.searchsorted(after, side="right"))
    from execution import TIMEFRAMES, bar_seconds
    cfg = Settings(**frozen["settings"])
    required = 120 * (TIMEFRAMES[frozen["hypothesis"].get("timeframe", "1d")] // bar_seconds(data))
    end = confirmation_end(data, frozen["hypothesis"], cfg, start)
    if end is None:
        return {"status": "PENDING", "passed": False, "bars": len(data)-start,
                "required_bars": required, "minimum_days": cfg.forward_min_days, "reason": "Esperando el horizonte prospectivo congelado"}
    # Do not keep expanding the window until it happens to pass.
    data = data.iloc[:end]
    claim = path.parent / "forward_validation.started"
    with claim.open("x", encoding="utf-8") as stream:
        stream.write(signature)
    result = evaluate_frozen(data, frozen["hypothesis"], Settings(**frozen["settings"]), start)
    result.update(candidate_sha256=signature, evidence_stage="FORWARD_PRICE_SIMULATION",
                  reason="Prueba prospectiva completada con reglas congeladas; ejecución en bróker pendiente",
                  execution_validation="PENDING_MT5_AND_BROKER")
    result_path.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--research-data", type=Path, help="Series auxiliares con publicaciones nuevas y fechas de disponibilidad")
    parser.add_argument("--data-manifest", type=Path, help="Datos nuevos del mismo contrato local/intradía")
    args = parser.parse_args()
    if args.data_manifest:
        from market_data import load_market_manifest
        datasets, metadata, _ = load_market_manifest(args.data_manifest)
    else:
        datasets, metadata = load_public_data(args.candidate.parent.parent / "market_cache")
    evidence, _ = load_evidence_series(args.research_data or args.candidate.parent / "research_data")
    print(json.dumps(evaluate_candidate(args.candidate, datasets, evidence, metadata), indent=2))


if __name__ == "__main__":
    main()
