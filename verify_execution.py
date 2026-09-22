"""Compare MT5/exported observations with frozen Python reference files.

No orders are sent. Missing trades mean PARTIAL, never execution certification.
Usage: python verify_execution.py EA_DIRECTORY --signals shadow.csv --trades mt5_trades.csv
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


def read_trace(path):
    frame = pd.read_csv(path, sep=None, engine="python")
    if "signal_timestamp" in frame:
        frame["timestamp"] = frame.signal_timestamp
    for column in ("timestamp", "entry_timestamp", "exit_timestamp"):
        if column in frame:
            frame[column] = pd.to_datetime(frame[column], unit="s", utc=True) if pd.api.types.is_numeric_dtype(frame[column]) else pd.to_datetime(frame[column], utc=True)
    if "direction" in frame:
        frame["direction"] = frame.direction.replace({"long": 1, "short": -1}).astype(int)
    return frame


def compare_frames(reference, observed, keys, numeric, exact, atol):
    columns = [*keys, *numeric, *exact]
    if set(columns)-set(reference) or set(columns)-set(observed):
        return {"passed": False, "reason": "Missing required trace columns"}
    if reference.duplicated(keys).any() or observed.duplicated(keys).any():
        return {"passed": False, "reason": "Duplicate observations"}
    expected = reference.set_index(keys).sort_index()
    actual = observed.set_index(keys).sort_index()
    if not len(expected) or not expected.index.equals(actual.index):
        return {"passed": False, "reason": "Missing/extra timestamps or empty reference", "expected_rows": len(expected), "observed_rows": len(actual)}
    mismatches = {}
    for column in numeric:
        a, b = expected[column].to_numpy(float), actual[column].to_numpy(float)
        bad = ~np.isfinite(a) | ~np.isfinite(b) | ~np.isclose(a, b, rtol=0, atol=atol)
        mismatches[column] = int(bad.sum())
    for column in exact:
        mismatches[column] = int((expected[column] != actual[column]).sum())
    return {"passed": not any(mismatches.values()), "rows": len(expected), "mismatches": mismatches, "absolute_tolerance": atol}


def verify(folder, signals_path, trades_path=None, price_tolerance=1e-8, pnl_tolerance=.01):
    if not all(np.isfinite(x) and x >= 0 for x in (price_tolerance, pnl_tolerance)):
        raise ValueError("Tolerancias deben ser finitas y no negativas")
    folder = Path(folder)
    manifest = json.loads((folder/"manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        if Path(name).name != name or hashlib.sha256((folder/name).read_bytes()).hexdigest() != expected:
            raise ValueError("Artefacto alterado: " + name)
    signals = compare_frames(read_trace(folder/"reference_signals.csv"), read_trace(signals_path),
                             ["timestamp"], [], ["entry", "exit", "direction"], 0)
    result = {"candidate": manifest["hypothesis_signature"], "signals": signals,
              "status": "PARTIAL" if signals["passed"] else "FAILED",
              "limitation": "Trace agreement only; does not authorize live trading or prove future liquidity"}
    if trades_path:
        reference, observed = read_trace(folder/"reference_trades.csv"), read_trace(trades_path)
        prices = compare_frames(reference, observed, ["entry_timestamp", "exit_timestamp"],
                                ["quantity", "entry_price", "exit_price"], ["direction"], price_tolerance)
        pnl = compare_frames(reference, observed, ["entry_timestamp", "exit_timestamp"], ["pnl"], [], pnl_tolerance)
        result.update(trades=prices, pnl=pnl, status="MATCHED" if signals["passed"] and prices["passed"] and pnl["passed"] else "FAILED")
    result["input_hashes"] = {"signals": hashlib.sha256(Path(signals_path).read_bytes()).hexdigest()}
    if trades_path:
        result["input_hashes"]["trades"] = hashlib.sha256(Path(trades_path).read_bytes()).hexdigest()
    (folder/"parity_report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--signals", type=Path, required=True)
    parser.add_argument("--trades", type=Path)
    parser.add_argument("--price-tolerance", type=float, default=1e-8)
    parser.add_argument("--pnl-tolerance", type=float, default=.01)
    args = parser.parse_args()
    result = verify(args.folder, args.signals, args.trades, args.price_tolerance, args.pnl_tolerance)
    print(json.dumps(result, indent=2))
    return 1 if result["status"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
