"""Persistent experiments; demo and real research use separate databases."""
import json
import sqlite3
import hashlib
from datetime import datetime, timezone
from contextlib import closing


class ResearchMemory:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS experiments (id INTEGER PRIMARY KEY, run TEXT, attempt INTEGER, scope TEXT, signature TEXT, structure TEXT, hypothesis TEXT, dossier TEXT, outcome TEXT DEFAULT 'PENDING', slots INTEGER, UNIQUE(scope, signature), UNIQUE(run, attempt))")
            db.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, run TEXT, attempt INTEGER, dossier TEXT, UNIQUE(run, attempt))")
            db.execute("CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL UNIQUE)")
            db.execute("CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END")
            db.execute("CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END")
            db.execute("PRAGMA user_version=2")
            if not db.execute("SELECT 1 FROM audit_events LIMIT 1").fetchone():
                self._append(db, "MIGRATION_BASELINE", {"experiments": db.execute("SELECT * FROM experiments ORDER BY id").fetchall(),
                                                       "notes": db.execute("SELECT * FROM notes ORDER BY id").fetchall()})
        self.verify()

    @staticmethod
    def _append(db, kind, payload):
        previous = db.execute("SELECT event_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        previous = previous[0] if previous else "0" * 64
        created = datetime.now(timezone.utc).isoformat()
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        signature = hashlib.sha256(json.dumps([created, kind, raw, previous], ensure_ascii=False).encode()).hexdigest()
        db.execute("INSERT INTO audit_events(created_at,kind,payload,previous_hash,event_hash) VALUES (?,?,?,?,?)",
                   (created, kind, raw, previous, signature))

    def append(self, kind, payload):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._append(db, kind, payload)

    def verify(self):
        previous = "0" * 64
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT created_at,kind,payload,previous_hash,event_hash FROM audit_events ORDER BY id").fetchall()
        for created, kind, raw, parent, signature in rows:
            expected = hashlib.sha256(json.dumps([created, kind, raw, parent], ensure_ascii=False).encode()).hexdigest()
            if parent != previous or expected != signature:
                raise ValueError("Research Ledger: cadena de auditoría corrupta")
            previous = signature
        return {"events": len(rows), "head": previous}

    def export_audit(self, path):
        """Portable audit copy. Independent custody is needed to detect full deletion."""
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        from pathlib import Path
        Path(path).write_text(json.dumps({"events": len(rows), "head": rows[-1][-1] if rows else "0"*64,
                                         "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")

    def recent(self):
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT n.dossier, e.scope FROM notes n LEFT JOIN experiments e ON n.run=e.run AND n.attempt=e.attempt ORDER BY n.id DESC LIMIT 12").fetchall()
        return [{**json.loads(row[0]), "context_id": row[1]} for row in rows]

    def structure_history(self, scope, structure):
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT hypothesis, outcome FROM experiments WHERE scope=? AND structure=? ORDER BY id", (scope, structure)).fetchall()
        return [(json.loads(h), outcome) for h, outcome in rows]

    @staticmethod
    def _trial_count(db):
        """The append-only log, not the mutable result projection, owns the budget."""
        total = 0
        for kind, raw in db.execute("SELECT kind,payload FROM audit_events WHERE kind IN ('MIGRATION_BASELINE','TRIAL_RESERVED') ORDER BY id"):
            payload = json.loads(raw)
            total += sum(row[-1] for row in payload["experiments"]) if kind == "MIGRATION_BASELINE" else payload["slots"]
        return total

    def reserve(self, run, attempt, scope, signature, structure, hypothesis, slots, limit=3):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM experiments WHERE scope=? AND signature=?", (scope, signature)).fetchone():
                return None
            rows = db.execute("SELECT outcome FROM experiments WHERE scope=? AND structure=?", (scope, structure)).fetchall()
            if limit is not None and len(rows) >= limit:
                raise ValueError("Presupuesto agotado: máximo tres configuraciones por estructura y contexto")
            if limit is not None and any(row[0] != "REJECTED" for row in rows):
                raise ValueError("La estructura tiene una evaluación pendiente o aprobada; no corresponde reformularla")
            base = self._trial_count(db)
            db.execute("INSERT INTO experiments(run,attempt,scope,signature,structure,hypothesis,dossier,slots) VALUES (?,?,?,?,?,?,?,?)",
                       (run, attempt, scope, signature, structure, json.dumps(hypothesis), '{}', slots))
            self._append(db, "TRIAL_RESERVED", {"run": run, "attempt": attempt, "scope": scope,
                         "signature": signature, "hypothesis": hypothesis, "slots": slots, "previous_trials": base})
            return base

    def save(self, run, attempt, dossier, outcome=None):
        payload = json.dumps(dossier, ensure_ascii=False, allow_nan=False)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._append(db, "RESEARCH_RESULT", {"run": run, "attempt": attempt, "outcome": outcome, "dossier": dossier})
            db.execute("INSERT INTO notes(run,attempt,dossier) VALUES (?,?,?) ON CONFLICT(run,attempt) DO UPDATE SET dossier=excluded.dossier", (run, attempt, payload))
            if outcome:
                db.execute("UPDATE experiments SET dossier=?,outcome=? WHERE run=? AND attempt=?", (payload, outcome, run, attempt))


def diagnose(row):
    """Actionable categories, without passing OOS returns to the researcher."""
    q, s = row.get("quant_metrics", {}), row.get("stress_metrics", {})
    reasons = q.get("rejection_reasons", []) + s.get("rejection_reasons", [])
    categories = []
    text = ' '.join(reasons).lower()
    if any(v.get("skipped_orders", 0) > 0 and v.get("trades", 0) == 0 for v in s.get("scenarios", {}).values()):
        categories.append(("execution_limits", "Revisar capital y mínimos del instrumento; no cambiar indicadores para resolver falta de capital."))
    if "operaciones" in text or "muestra" in text:
        categories.append(("insufficient_evidence", "Investigar si faltan datos o si las entradas son demasiado escasas; no concluir ausencia de señal."))
    if "costos" in text:
        categories.append(("costs", "Examinar rotación y horizonte; justificar cualquier reducción de frecuencia con datos de entrenamiento."))
    if "parámetros" in text:
        categories.append(("parameter_fragility", "Buscar estabilidad en entrenamiento, no optimizar el resultado OOS."))
    if any(word in text for word in ("walk-forward", "drawdown", "monte carlo", "degradación")):
        categories.append(("instability", "Revisar dependencia del régimen y exposición al riesgo en entrenamiento."))
    if any(word in text for word in ("dsr", "evidencia", "sharpe", "retorno", "profit factor")):
        categories.append(("weak_evidence", "Revisar el mecanismo y su comparación base; no hacer ajustes cosméticos."))
    if not categories and reasons:
        categories.append(("other", "Revisar el motivo documentado antes de proponer otra hipótesis."))
    return [{"category": key, "next_step": action} for key, action in categories]
