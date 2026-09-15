"""Agente LangGraph de investigación; exporta simuladores autónomos, no órdenes live.

Python 3.11+. CSV: timestamp,open,high,low,close (precios positivos, USD).
Ejemplo offline: python trading_agent.py --demo
Ejemplo LLM: python trading_agent.py --csv bars.csv --model MODELO --bars-per-year 252
DSR es una probabilidad: se exige >= .95 y su estadístico z > 1.2.
El OOS reutilizado es validación adaptativa, NO una prueba final independiente.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import logging
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import List, Literal, TypedDict

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator
from langgraph.graph import END, START, StateGraph

LOGGER = logging.getLogger("quant_agent")


class AgentState(TypedDict):
    hypothesis: dict
    prototype_code: str
    quant_metrics: dict
    stress_metrics: dict
    production_code: str
    iteration_count: int
    logs: List[str]
    status: str
    history: List[dict]


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family: Literal["trend", "mean_reversion", "breakout", "momentum", "dip"]
    rationale: str = Field(min_length=30)
    fast: int = Field(ge=2, le=40)
    slow: int = Field(ge=10, le=150)
    threshold: float = Field(ge=0.1, le=3)
    allocation: float = Field(gt=0, le=0.95)
    stop_loss: float = Field(ge=0.005, le=0.15)
    take_profit: float = Field(ge=0.01, le=0.40)
    max_holding: int = Field(ge=2, le=100)

    @model_validator(mode="after")
    def ordered(self):
        if self.fast >= self.slow:
            raise ValueError("fast debe ser menor que slow")
        return self


class CodeNotes(BaseModel):
    summary: str
    operational_notes: List[str]


@dataclass(frozen=True)
class Settings:
    max_iterations: int = 10
    bars_per_year: int = 252
    commission_rate: float = 0.001
    fixed_commission: float = 0.0
    spread_bps: float = 5.0
    slippage_bps: float = 5.0
    min_notional: float = 5.0
    quantity_step: float = 0.00001
    min_trades: int = 30
    max_drawdown: float = 0.25
    dsr_probability_min: float = 0.95
    dsr_z_min: float = 1.2
    bootstrap_samples: int = 200
    seed: int = 42

    def __post_init__(self):
        if not 1 <= self.max_iterations <= 10:
            raise ValueError("max_iterations debe estar entre 1 y 10")
        values = asdict(self)
        if any(not math.isfinite(v) or v < 0 for v in values.values()):
            raise ValueError("Configuración negativa o no finita")
        if self.quantity_step <= 0 or self.bars_per_year < 1 or self.min_trades < 1:
            raise ValueError("Paso, frecuencia y mínimo de operaciones deben ser positivos")
        if self.bootstrap_samples < 50 or not 0 < self.max_drawdown <= 0.25:
            raise ValueError("Bootstrap >= 50 y drawdown en (0, .25]")
        if not 0 < self.dsr_probability_min < 1:
            raise ValueError("El DSR probabilístico está entre 0 y 1; 1.2 es un z-score")
        if self.commission_rate >= 1 or (self.spread_bps / 2 + self.slippage_bps) >= 10000:
            raise ValueError("Fricciones incompatibles con precios de ejecución positivos")


def validate_data(frame: pd.DataFrame, minimum: int = 600) -> pd.DataFrame:
    frame = frame.copy()
    frame.columns = frame.columns.str.lower()
    required = ["timestamp", "open", "high", "low", "close"]
    if not set(required).issubset(frame.columns):
        raise ValueError(f"CSV requiere {required}")
    frame = frame[required]
    frame["timestamp"] = pd.to_datetime(frame.timestamp, utc=True, errors="raise")
    if frame.timestamp.isna().any() or frame.timestamp.duplicated().any() or not frame.timestamp.is_monotonic_increasing:
        raise ValueError("Fechas deben ser válidas, únicas y ordenadas")
    price = frame[["open", "high", "low", "close"]].astype(float)
    if not np.isfinite(price.to_numpy()).all() or (price <= 0).any().any():
        raise ValueError("Precios no finitos o no positivos")
    if ((price.high < price.max(axis=1)) | (price.low > price.min(axis=1))).any():
        raise ValueError("OHLC inconsistente")
    if len(frame) < minimum:
        raise ValueError(f"Se requieren al menos {minimum} barras")
    frame[price.columns] = price
    return frame.reset_index(drop=True)


def signals(data: pd.DataFrame, h: dict) -> np.ndarray:
    """Decisión al cierre t. El motor sólo la ejecuta en open[t+1]."""
    c = data.close
    fast, slow = c.rolling(h["fast"]).mean(), c.rolling(h["slow"]).mean()
    family = h["family"]
    if family == "trend":
        active = fast > slow
    elif family == "mean_reversion":
        z = (c - slow) / c.rolling(h["slow"]).std().replace(0, np.nan)
        active = z < -h["threshold"]
    elif family == "breakout":
        active = c > data.high.rolling(h["slow"]).max().shift(1)
    elif family == "momentum":
        active = c.pct_change(h["slow"]) > h["threshold"] / 100
    elif family == "dip":
        active = (c > slow) & (c < fast)
    else:
        raise ValueError("Familia no soportada")
    return active.fillna(False).to_numpy(dtype=bool)


def backtest(data: pd.DataFrame, h: dict, cfg: Settings, capital: float,
             start: int = 0, end: int | None = None, cost_multiplier: float = 1.0) -> dict:
    """Long-only, sin apalancamiento. Stops al cierre, ejecutados en próxima apertura.

    No supone fills intrabar ni usa high/low futuros. Liquida al cierre final con costos.
    Cada segmento empieza en efectivo; el historial previo sólo sirve de calentamiento.
    """
    end = len(data) if end is None else end
    if not 0 <= start < end <= len(data) or capital <= 0:
        raise ValueError("Segmento o capital inválido")
    sig = signals(data, h)
    friction = (cfg.spread_bps / 2 + cfg.slippage_bps) * cost_multiplier / 10000
    rate, fixed = cfg.commission_rate * cost_multiplier, cfg.fixed_commission * cost_multiplier
    if friction >= 1 or rate >= 1:
        raise ValueError("Costos de estrés excesivos")
    cash, qty, entry, debit = capital, 0.0, 0.0, 0.0
    age, skipped = 0, 0
    equity, trades = [capital], []
    o, c = data.open.to_numpy(), data.close.to_numpy()
    for i in range(start, end):
        sold = False
        if qty:
            age += 1
            previous = c[i - 1]
            exit_now = (not sig[i - 1] or previous <= entry * (1 - h["stop_loss"])
                        or previous >= entry * (1 + h["take_profit"])
                        or age >= h["max_holding"])
            if exit_now:
                gross = qty * o[i] * (1 - friction)
                proceeds = gross * (1 - rate) - fixed
                cash += proceeds
                trades.append(proceeds - debit)
                qty, sold = 0.0, True
        if not qty and not sold and i > 0 and sig[i - 1] and cash > 0:
            price = o[i] * (1 + friction)
            budget = cash * h["allocation"]
            units = max(0.0, (budget - fixed) / (price * (1 + rate)))
            candidate = math.floor(units / cfg.quantity_step) * cfg.quantity_step
            if candidate > 0 and candidate * price >= cfg.min_notional:
                debit = candidate * price * (1 + rate) + fixed
                cash -= debit
                qty, entry, age = candidate, price, 0
            else:
                skipped += 1
        equity.append(cash + qty * c[i])
    if qty:
        proceeds = qty * c[end - 1] * (1 - friction) * (1 - rate) - fixed
        cash += proceeds
        trades.append(proceeds - debit)
        equity[-1] = cash
    curve = np.asarray(equity, dtype=float)
    returns = np.diff(curve) / np.maximum(curve[:-1], 1e-12)
    deviation = returns.std(ddof=1) if len(returns) > 1 else 0.0
    sr = float(returns.mean() / deviation) if deviation > 1e-12 else 0.0
    gains = sum(max(t, 0) for t in trades)
    losses = -sum(min(t, 0) for t in trades)
    return {"returns": returns, "equity": curve, "sharpe": sr * math.sqrt(cfg.bars_per_year),
            "net_return": float(curve[-1] / capital - 1), "final_equity": float(curve[-1]),
            "max_drawdown": float(np.max(1 - curve / np.maximum.accumulate(curve))),
            "profit_factor": gains / losses if losses else (None if gains else 0.0),
            "no_losing_trades": bool(gains and not losses), "trades": len(trades),
            "skipped_orders": skipped, "bankrupt": bool(np.min(curve) <= 0)}


def compact(result: dict) -> dict:
    return {k: v for k, v in result.items() if k not in ("returns", "equity")}


def deflated_sharpe(returns: np.ndarray, cfg: Settings) -> dict:
    """Bailey/Lopez de Prado; null SR dispersion estimated by circular block bootstrap.

    Full planned trial budget is charged from attempt one. This is an approximation,
    not an exact correction for unrestricted adaptive LLM research or repeated runs.
    The block bootstrap also reduces the effective sample size for dependence.
    """
    r = np.asarray(returns, dtype=float)
    n, sd = len(r), float(np.std(r, ddof=1))
    if n < 30 or not np.isfinite(r).all() or sd < 1e-12:
        return {"dsr": 0.0, "dsr_z": 0.0, "reason": "Muestra insuficiente o varianza cero"}
    sr = float(r.mean() / sd)
    centered = r - r.mean()
    rng = np.random.default_rng(cfg.seed)
    block = max(2, int(round(n ** (1 / 3))))
    null_srs = []
    for _ in range(cfg.bootstrap_samples):
        starts = rng.integers(0, n, size=math.ceil(n / block))
        idx = ((starts[:, None] + np.arange(block)) % n).ravel()[:n]
        sample = centered[idx]
        null_srs.append(float(sample.mean() / max(sample.std(ddof=1), 1e-12)))
    dispersion = float(np.std(null_srs, ddof=1))
    normal = NormalDist()
    trials = cfg.max_iterations
    benchmark = 0.0
    if trials > 1:
        gamma = 0.5772156649015329
        benchmark = dispersion * ((1 - gamma) * normal.inv_cdf(1 - 1 / trials)
                                  + gamma * normal.inv_cdf(1 - 1 / (trials * math.e)))
    standardized = centered / sd
    skew, kurtosis = float(np.mean(standardized ** 3)), float(np.mean(standardized ** 4))
    effective_n = min(float(n), 1 + 1 / max(dispersion ** 2, 1e-12))
    denominator = math.sqrt(max(1e-12, 1 - skew * sr + (kurtosis - 1) / 4 * sr ** 2))
    z = (sr - benchmark) * math.sqrt(effective_n - 1) / denominator
    return {"dsr": normal.cdf(z), "dsr_z": z, "sr_benchmark_per_bar": benchmark,
            "effective_observations": effective_n, "planned_trials": trials,
            "method": "block-bootstrap null dispersion; approximate DSR"}


def strategy_source(h: dict, cfg: Settings, notes: dict) -> str:
    """Export exact tested functions; LLM text is JSON data, never executable code."""
    header = ('from __future__ import annotations\nimport argparse, json, math, logging\n'
              'from dataclasses import dataclass, asdict\nimport numpy as np\nimport pandas as pd\n\n')
    definitions = "\n\n".join(inspect.getsource(f) for f in
                               (Settings, validate_data, signals, backtest, compact))
    payload = json.dumps({"hypothesis": h, "settings": asdict(cfg), "notes": notes}, ensure_ascii=False)
    runner = '''
def main():
    parser = argparse.ArgumentParser(description="Validated strategy: historical paper execution")
    parser.add_argument("--csv", required=True)
    parser.add_argument("--capital", type=float, default=100.0)
    args = parser.parse_args()
    data = validate_data(pd.read_csv(args.csv))
    result = backtest(data, PAYLOAD["hypothesis"], Settings(**PAYLOAD["settings"]), args.capital)
    print(json.dumps(compact(result), indent=2, allow_nan=False))

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        main()
    except Exception:
        logging.exception("Strategy execution failed")
        raise SystemExit(1)
'''
    source = header + definitions + "\nPAYLOAD = json.loads(" + repr(payload) + ")\n" + runner
    compile(source, "strategy.py", "exec")
    return source


def initial_state() -> AgentState:
    return {"hypothesis": {}, "prototype_code": "", "quant_metrics": {}, "stress_metrics": {},
            "production_code": "", "iteration_count": 0, "logs": [], "status": "RESEARCHING", "history": []}


class TradingAgent:
    def __init__(self, data: pd.DataFrame, cfg: Settings, output: Path,
                 model: str | None = None, demo: bool = False):
        self.data, self.cfg, self.output, self.demo = validate_data(data), cfg, output, demo
        output.mkdir(parents=True, exist_ok=False)
        self.llm = None
        if not demo:
            if not os.environ.get("OPENAI_API_KEY") or not model:
                raise ValueError("Configura OPENAI_API_KEY y --model; para prueba offline usa --demo")
            from langchain_openai import ChatOpenAI
            self.llm = ChatOpenAI(model=model, timeout=60, max_retries=2)

    def log(self, state: AgentState, message: str) -> List[str]:
        LOGGER.info(message)
        return state["logs"] + [message]

    def researcher_node(self, state: AgentState) -> dict:
        attempt = state["iteration_count"] + 1
        reset = {"iteration_count": attempt, "status": "RESEARCHING", "hypothesis": {},
                 "prototype_code": "", "production_code": "", "quant_metrics": {}, "stress_metrics": {}}
        try:
            used = [row["hypothesis"]["family"] for row in state["history"] if row["hypothesis"]]
            if self.demo:
                families = ["trend", "mean_reversion", "breakout", "momentum", "dip"]
                h = Hypothesis(family=families[(attempt - 1) % len(families)],
                    rationale="Hipótesis de demostración: persistencia o reversión de precios; no evidencia de alfa.",
                    fast=10, slow=40, threshold=1.0, allocation=.9,
                    stop_loss=.03, take_profit=.06, max_holding=20)
            else:
                prompt = ("Propón una hipótesis financiera long-only sin apalancamiento. "
                          "Elige una familia NUEVA, no cambies sólo parámetros de una rechazada. "
                          "trend: SMA fast>slow; mean_reversion: z(close,slow)<-threshold; "
                          "breakout: close>máximo high de slow barras anteriores; "
                          "momentum: retorno slow>threshold/100; dip: close>SMA slow y close<SMA fast. "
                          "Entrada en próxima apertura; salida cuando condición deja de cumplirse, "
                          "stop/take detectado al cierre o max_holding. Explica fundamento y riesgo. "
                          f"Familias usadas: {used}. Feedback: {state['logs'][-6:]}")
                h = self.llm.with_structured_output(Hypothesis, method="json_schema").invoke(prompt)
            if h.family in used:
                raise ValueError("Familia repetida; no es una hipótesis distinta")
            reset["hypothesis"] = h.model_dump()
            reset["logs"] = self.log(state, f"Intento {attempt}: {h.family}; R/R nominal {h.take_profit/h.stop_loss:.2f}")
        except Exception as exc:
            reset["status"] = "REJECTED"
            reset["logs"] = self.log(state, f"Investigación rechazada: {type(exc).__name__}: {exc}")
        return reset

    def prototyper_node(self, state: AgentState) -> dict:
        if state["status"] == "REJECTED":
            return {}
        source = strategy_source(state["hypothesis"], self.cfg, {"stage": "prototype"})
        (self.output / f"prototype_{state['iteration_count']:02}.py").write_text(source, encoding="utf-8")
        return {"prototype_code": source}

    def quant_validator_node(self, state: AgentState) -> dict:
        if state["status"] == "REJECTED":
            return {}
        split = int(len(self.data) * .7)
        h, cfg = state["hypothesis"], self.cfg
        ins = backtest(self.data, h, cfg, 10000, h["slow"], split)
        oos = backtest(self.data, h, cfg, 10000, split)
        dsr = deflated_sharpe(oos["returns"], cfg)
        degradation = max(0.0, 1 - oos["sharpe"] / ins["sharpe"]) if ins["sharpe"] > 0 else 1.0
        boundaries = np.linspace(split, len(self.data), 4, dtype=int)
        folds = [compact(backtest(self.data, h, cfg, 10000, int(a), int(b)))
                 for a, b in zip(boundaries[:-1], boundaries[1:])]
        reasons = []
        if dsr["dsr"] < cfg.dsr_probability_min or dsr["dsr_z"] <= cfg.dsr_z_min:
            reasons.append("DSR insuficiente (probabilidad >= .95 y z > 1.2)")
        if ins["sharpe"] <= 0 or oos["sharpe"] <= 0 or degradation > .20:
            reasons.append("Sharpe no positivo o degradación OOS > 20%")
        if oos["trades"] < cfg.min_trades or ins["trades"] < cfg.min_trades:
            reasons.append("Operaciones insuficientes")
        if any(r["max_drawdown"] > cfg.max_drawdown or r["net_return"] <= 0 for r in (ins, oos)):
            reasons.append("Retorno o drawdown IS/OOS fuera de límites")
        if not (oos["no_losing_trades"] or (oos["profit_factor"] or 0) >= 1.2):
            reasons.append("Profit Factor OOS < 1.2")
        if sum(f["net_return"] > 0 for f in folds) < 2:
            reasons.append("Menos de 2/3 ventanas progresivas positivas")
        metrics = {"passed": not reasons, "in_sample": compact(ins), "out_of_sample": compact(oos),
                   **dsr, "oos_degradation": degradation, "forward_windows": folds,
                   "oos_is_adaptive": True, "rejection_reasons": reasons}
        history = state["history"] + [{"hypothesis": h, "quant_metrics": metrics}]
        return {"quant_metrics": metrics, "history": history,
                "status": "REJECTED" if reasons else "RESEARCHING",
                "logs": self.log(state, "Quant: " + ("; ".join(reasons) or "APROBADO"))}

    def stress_test_node(self, state: AgentState) -> dict:
        start = int(len(self.data) * .7)
        scenarios = {str(mult): compact(backtest(self.data, state["hypothesis"], self.cfg,
                                               100.0, start, cost_multiplier=mult)) for mult in (1, 2, 3)}
        reasons = []
        for name, result in scenarios.items():
            if (result["bankrupt"] or result["max_drawdown"] > self.cfg.max_drawdown
                    or result["net_return"] <= 0 or result["trades"] < self.cfg.min_trades):
                reasons.append(f"Costos x{name}: retorno, drawdown, quiebra o número de operaciones inválido")
        metrics = {"initial_capital": 100, "passed": not reasons, "scenarios": scenarios,
                   "rejection_reasons": reasons}
        history = [dict(row) for row in state["history"]]
        history[-1]["stress_metrics"] = metrics
        return {"stress_metrics": metrics, "status": "REJECTED" if reasons else "RESEARCHING",
                "history": history,
                "logs": self.log(state, "Estrés: " + ("; ".join(reasons) or "APROBADO"))}

    def production_coder_node(self, state: AgentState) -> dict:
        if not state["quant_metrics"].get("passed") or not state["stress_metrics"].get("passed"):
            raise RuntimeError("Producción requiere ambos filtros aprobados")
        if self.demo:
            return {"status": "REJECTED", "logs": self.log(state, "Datos sintéticos: exportación de producción bloqueada")}
        notes = self.llm.with_structured_output(CodeNotes, method="json_schema").invoke(
            "Documenta el módulo Python de simulación que compilará el generador determinista. "
            "No modifiques reglas ni afirmes validación live. Describe límites de OOS adaptativo, "
            "stops al cierre, costos supuestos y ausencia de bróker. Hipótesis y métricas: "
            + json.dumps({"h": state["hypothesis"], "q": state["quant_metrics"], "s": state["stress_metrics"]}))
        source = strategy_source(state["hypothesis"], self.cfg, notes.model_dump())
        (self.output / "production_strategy.py").write_text(source, encoding="utf-8")
        return {"production_code": source, "status": "APPROVED",
                "logs": self.log(state, "Exportado motor de simulación aprobado; validación independiente pendiente")}

    def should_continue_after_quant(self, state: AgentState) -> str:
        if state["quant_metrics"].get("passed") and state["status"] != "REJECTED":
            return "stress_test_node"
        return END if state["iteration_count"] >= self.cfg.max_iterations else "researcher_node"

    def should_continue_after_stress(self, state: AgentState) -> str:
        if state["stress_metrics"].get("passed"):
            return "production_coder_node"
        return END if state["iteration_count"] >= self.cfg.max_iterations else "researcher_node"

    def build_graph(self):
        graph = StateGraph(AgentState)
        for name in ("researcher_node", "prototyper_node", "quant_validator_node", "stress_test_node", "production_coder_node"):
            graph.add_node(name, getattr(self, name))
        graph.add_edge(START, "researcher_node")
        graph.add_edge("researcher_node", "prototyper_node")
        graph.add_edge("prototyper_node", "quant_validator_node")
        graph.add_conditional_edges("quant_validator_node", self.should_continue_after_quant)
        graph.add_conditional_edges("stress_test_node", self.should_continue_after_stress)
        graph.add_edge("production_coder_node", END)
        return graph.compile()

    def run(self) -> AgentState:
        state = initial_state()
        metadata = {"settings": asdict(self.cfg), "synthetic": self.demo,
                    "data_sha256": hashlib.sha256(self.data.to_csv(index=False).encode()).hexdigest(),
                    "warning": "OOS adaptativo; reservar nuevos datos antes de uso real"}
        (self.output / "manifest.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        try:
            for state in self.build_graph().stream(state, stream_mode="values",
                                                  config={"recursion_limit": self.cfg.max_iterations * 5 + 5}):
                self.save(state)
        except Exception as exc:
            state = {**state, "status": "REJECTED", "production_code": "",
                     "logs": self.log(state, f"Fallo operativo: {type(exc).__name__}: {exc}")}
            self.save(state)
            raise
        if state["status"] == "REJECTED" and state["iteration_count"] >= self.cfg.max_iterations:
            state["logs"] = self.log(state, "Límite de intentos alcanzado: ejecución detenida")
        self.save(state)
        return state

    def save(self, state: AgentState):
        text = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False)
        temporary = self.output / "state.tmp"
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(self.output / "state.json")


def demo_data(seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, .012, 2200)))
    opening = np.r_[close[0], close[:-1]] * np.exp(rng.normal(0, .002, len(close)))
    return pd.DataFrame({"timestamp": pd.date_range("2016-01-01", periods=len(close), freq="B"),
                         "open": opening, "close": close,
                         "high": np.maximum(opening, close) * 1.005,
                         "low": np.minimum(opening, close) * .995})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", type=Path)
    source.add_argument("--demo", action="store_true")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL"))
    parser.add_argument("--settings", type=Path, help="JSON con campos de Settings")
    parser.add_argument("--bars-per-year", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    settings = json.loads(args.settings.read_text(encoding="utf-8")) if args.settings else {}
    if args.bars_per_year:
        settings["bars_per_year"] = args.bars_per_year
    cfg = Settings(**settings)
    data = demo_data(cfg.seed) if args.demo else pd.read_csv(args.csv)
    output = args.output or Path("outputs") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    result = TradingAgent(data, cfg, output, args.model, args.demo).run()
    print(json.dumps({"status": result["status"], "attempts": result["iteration_count"],
                      "artifacts": str(output.resolve())}, indent=2))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except Exception:
        LOGGER.exception("La ejecución no pudo completarse")
        raise SystemExit(1)
