"""Persistent experiments; demo and real research use separate databases."""
import json
import sqlite3
from contextlib import closing


class ResearchMemory:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS experiments (id INTEGER PRIMARY KEY, run TEXT, attempt INTEGER, scope TEXT, signature TEXT, structure TEXT, hypothesis TEXT, dossier TEXT, outcome TEXT DEFAULT 'PENDING', slots INTEGER, UNIQUE(scope, signature), UNIQUE(run, attempt))")
            db.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, run TEXT, attempt INTEGER, dossier TEXT, UNIQUE(run, attempt))")

    def recent(self):
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT n.dossier, e.scope FROM notes n LEFT JOIN experiments e ON n.run=e.run AND n.attempt=e.attempt ORDER BY n.id DESC LIMIT 12").fetchall()
        return [{**json.loads(row[0]), "context_id": row[1]} for row in rows]

    def structure_history(self, scope, structure):
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT hypothesis, outcome FROM experiments WHERE scope=? AND structure=? ORDER BY id", (scope, structure)).fetchall()
        return [(json.loads(h), outcome) for h, outcome in rows]

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
            base = db.execute("SELECT COALESCE(SUM(slots), 0) FROM experiments").fetchone()[0]
            db.execute("INSERT INTO experiments(run,attempt,scope,signature,structure,hypothesis,dossier,slots) VALUES (?,?,?,?,?,?,?,?)",
                       (run, attempt, scope, signature, structure, json.dumps(hypothesis), '{}', slots))
            return base

    def save(self, run, attempt, dossier, outcome=None):
        payload = json.dumps(dossier, ensure_ascii=False, allow_nan=False)
        with closing(sqlite3.connect(self.path)) as db, db:
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
