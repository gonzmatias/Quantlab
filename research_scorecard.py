"""Inventory every frozen candidate, including failed and pending forward tests."""
import argparse
import json
from pathlib import Path

from experiment_protocol import digest


def scorecard(root):
    rows = []
    for path in sorted(Path(root).glob("*/candidate.json")):
        frozen = json.loads(path.read_text(encoding="utf-8"))
        signature = frozen.pop("sha256")
        if digest(frozen) != signature:
            raise ValueError("Candidato alterado: " + str(path))
        forward = path.parent/"forward_validation.json"
        result = json.loads(forward.read_text(encoding="utf-8")) if forward.exists() else {}
        if result and result.get("candidate_sha256") != signature:
            raise ValueError("Validación asociada a otro candidato")
        metrics = result.get("scenarios", {}).get("1", {})
        rows.append({"run": path.parent.name, "candidate": signature, "asset": frozen["asset"],
                     "frozen_at": frozen["frozen_at"], "status": result.get("status", "PENDING"),
                     "passed": result.get("passed"), "forward_sharpe": metrics.get("sharpe"),
                     "forward_return": metrics.get("net_return"),
                     "engine_hashes": frozen.get("engine_hashes", {})})
    completed = [r for r in rows if r["passed"] is not None]
    return {"frozen_candidates": len(rows), "completed": len(completed), "pending": len(rows)-len(completed),
            "passed": sum(bool(r["passed"]) for r in completed), "candidates": rows,
            "limitation": "Descriptive inventory; correlated candidates are not independent trials. Missing folders require externally held ledger reconciliation."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, nargs="?", default=Path("outputs"))
    print(json.dumps(scorecard(parser.parse_args().root), indent=2))
