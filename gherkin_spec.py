"""Versioned Gherkin step definitions. Expected answers always come from the author."""
from __future__ import annotations

import json
import re
from numbers import Real
from typing import Literal

import numpy as np
import pandas as pd
from gherkin.parser import Parser
from pydantic import BaseModel, ConfigDict, Field, model_validator

from execution import TIMEFRAMES, bar_seconds, scheduled_signals
from strategy_rules import RuleProgram, program_signals
from trading_agent import Hypothesis, Settings, backtest, signals, validate_data
from tick_data import TICK_MODEL, RETURN_SECONDS, TickExecution, validate_ticks


class SpecError(ValueError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Evaluation(StrictModel):
    start: str
    end: str
    protocol: Literal["fixed-holdout-v1"]
    settings: dict

    @model_validator(mode="after")
    def validate_settings(self):
        start, end = pd.Timestamp(self.start), pd.Timestamp(self.end)
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError("Evaluación: fechas con zona horaria y start < end; end exclusivo")
        Settings(**self.settings)
        return self


class Contract(StrictModel):
    version: Literal[1, 2]
    asset: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    instrument_type: str
    quote_currency: str
    source: str = Field(min_length=1)
    contract: str = Field(min_length=1)
    data_timeframe: str
    timeframe: str
    direction: str
    allocation: float = Field(gt=0, le=1)
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    max_holding: int = Field(strict=True, ge=1)
    execution_model: str
    costs: dict
    evaluation: Evaluation
    tick_execution: TickExecution | None = None


EXECUTION_MODEL = "bar_open_market; stops_at_signal_close; estimated_fills"
COST_FIELDS = {"bars_per_year", "quote_side", "commission_rate", "fixed_commission",
               "spread_bps", "slippage_bps", "quantity_step", "min_notional",
               "holding_cost_bps", "allow_short"}


def capabilities(contract):
    c = contract
    missing = []
    is_tick = c.version == 2
    if c.execution_model != (TICK_MODEL if is_tick else EXECUTION_MODEL):
        missing.append("execution_model=" + c.execution_model)
    if is_tick and c.tick_execution is None:
        raise SpecError("INVALID_SPEC", "Contrato v2 requiere tick_execution completo")
    if not is_tick and (c.tick_execution is not None or c.stop_loss >= 1 or c.max_holding > 10000):
        raise SpecError("INVALID_SPEC", "Campos incompatibles con contrato de barras v1")
    if c.quote_currency != "USD" or c.instrument_type not in ("spot", "fx", "cfd"):
        missing.append("contrato/moneda " + c.instrument_type + "/" + c.quote_currency)
    if c.direction not in ("long", "short"):
        missing.append("dirección " + c.direction)
    base, target = TIMEFRAMES.get(c.data_timeframe), TIMEFRAMES.get(c.timeframe)
    if is_tick:
        if c.data_timeframe != "tick" or (c.timeframe != "tick" and not target):
            missing.append("Resolución tick/temporalidad de señal no disponible")
    elif not base or not target or target < base or target % base:
        missing.append("resolución " + c.data_timeframe + " → " + c.timeframe)
    if set(c.costs) != COST_FIELDS:
        raise SpecError("INVALID_SPEC", "costs requiere exactamente: " + ", ".join(sorted(COST_FIELDS)))
    if COST_FIELDS & set(c.evaluation.settings):
        raise SpecError("INVALID_SPEC", "Los costos se definen solo en costs, sin duplicarlos en evaluation.settings")
    cfg = Settings(**c.evaluation.settings, **c.costs)
    if cfg.execution_delay_bars:
        missing.append("execution_delay_bars distinto de cero")
    if c.direction == "short" and (not cfg.allow_short or c.instrument_type == "spot"):
        missing.append("short sin contrato habilitado")
    if is_tick:
        if cfg.spread_bps != 0 or cfg.quote_side != 0:
            raise SpecError("INVALID_SPEC", "Ticks usan bid/ask observado: spread_bps=0 y quote_side=0")
        expected_frequency = 365 * 86400 // RETURN_SECONDS[c.tick_execution.return_interval]
        if cfg.bars_per_year != expected_frequency:
            raise SpecError("INVALID_SPEC", f"Retornos en rejilla calendario requieren bars_per_year={expected_frequency}")
        if c.tick_execution.distance_unit == "percent" and (c.stop_loss >= 1 or (c.direction == "short" and c.take_profit >= 1)):
            raise SpecError("INVALID_SPEC", "Distancia porcentual produce stop/objetivo no positivo")
    if missing:
        raise SpecError("NEEDS_CAPABILITY", "Capacidades no disponibles: " + "; ".join(missing))
    return cfg


def json_value(text):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError("Clave JSON duplicada: " + key)
            obj[key] = value
        return obj
    def invalid(value):
        raise ValueError("Número JSON no finito: " + value)
    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)


def body(step):
    if "docString" not in step or "dataTable" in step:
        raise SpecError("INVALID_SPEC", "El paso requiere DocString: " + step["text"])
    return step["docString"]["content"]


def parse_markdown(text):
    if not isinstance(text, str) or not 1 <= len(text) <= 250000:
        raise SpecError("INVALID_SPEC", "Documento vacío o mayor que 250000 caracteres")
    # Only the fenced specification is normative; surrounding Markdown is commentary.
    blocks = re.findall(r"^```gherkin\s*\n(.*?)^```\s*$", text, re.M | re.S)
    if len(blocks) != 1:
        raise SpecError("INVALID_SPEC", "Se requiere exactamente un bloque ```gherkin en el Markdown")
    try:
        feature = Parser().parse(blocks[0])["feature"]
    except Exception as exc:
        raise SpecError("INVALID_SPEC", "Gherkin inválido: " + str(exc)) from exc
    children = feature["children"]
    if feature.get("description", "").strip() or feature.get("tags"):
        raise SpecError("NEEDS_CAPABILITY", "Descripciones y etiquetas dentro de Gherkin no tienen semántica ejecutable; usa los pasos definidos")
    if not children or "background" not in children[0]:
        raise SpecError("INVALID_SPEC", "Se requieren Antecedentes con contrato, entrada y salida")
    background = children[0]["background"]
    if background.get("description", "").strip():
        raise SpecError("NEEDS_CAPABILITY", "Antecedentes contiene texto sin paso ejecutable")
    steps = background["steps"]
    names = [s["text"] for s in steps]
    if names != ["el contrato", "la regla de entrada", "la regla de salida"]:
        raise SpecError("NEEDS_CAPABILITY", "Pasos de Antecedentes no soportados: " + repr(names))
    contract = Contract.model_validate(json_value(body(steps[0])))
    # Validate capabilities before spending API tokens.
    capabilities(contract)
    rules = [json_value(body(step)) for step in steps[1:]]
    for rule in rules:
        if (not isinstance(rule, dict) or set(rule) - {"text", "expression"}
                or not isinstance(rule.get("text"), str) or not rule["text"].strip()
                or ("expression" in rule and (not isinstance(rule["expression"], str) or not rule["expression"].strip()))):
            raise SpecError("INVALID_SPEC", "Regla requiere text y opcionalmente expression normativa")
    scenarios = []
    for child in children[1:]:
        if "scenario" not in child or child["scenario"].get("examples"):
            raise SpecError("NEEDS_CAPABILITY", "Solo Escenario concreto; Rule/Examples requieren adaptador")
        scenario = child["scenario"]
        if scenario.get("description", "").strip() or scenario.get("tags"):
            raise SpecError("NEEDS_CAPABILITY", "Escenario contiene descripción o etiquetas sin implementación")
        steps = scenario["steps"]
        names = [s["text"] for s in steps]
        if (len(steps) != 3 or names[0] != ("los ticks" if contract.version == 2 else "las barras") or names[2] != "el resultado exacto"
                or names[1] not in ("calculo las señales", "ejecuto la estrategia")):
            raise SpecError("NEEDS_CAPABILITY", "Pasos sin implementación en escenario: " + scenario["name"])
        if "docString" in steps[1] or "dataTable" in steps[1]:
            raise SpecError("INVALID_SPEC", "El paso Cuando no acepta argumentos adicionales")
        scenarios.append({"name": scenario["name"], "line": scenario["location"]["line"],
                          "mode": "signals" if names[1] == "calculo las señales" else "execution",
                          "bars": json_value(body(steps[0])), "expected": json_value(body(steps[2]))})
    if {s["mode"] for s in scenarios} != {"signals", "execution"}:
        raise SpecError("INVALID_SPEC", "Incluye escenarios de señales y de ejecución con resultados esperados")
    if len({s["name"] for s in scenarios}) != len(scenarios):
        raise SpecError("INVALID_SPEC", "Nombres de escenario repetidos")
    if len(scenarios) > 100 or any(not isinstance(s["bars"], list) or not 2 <= len(s["bars"]) <= 10000 for s in scenarios):
        raise SpecError("INVALID_SPEC", "Máximo 100 escenarios, cada uno con 2–10000 barras")
    return {"name": feature["name"], "contract": contract.model_dump(), "rules": rules, "scenarios": scenarios}


def hypothesis(document, program):
    c = document["contract"]
    if c["version"] == 2:
        from strategy_rules import program_requirements
        p = RuleProgram.model_validate(program).model_dump()
        return {**{k: c[k] for k in ("timeframe", "direction", "allocation", "stop_loss", "take_profit", "max_holding")},
                "family": document["name"], "program": p, "slow": max(1, program_requirements(p)[1])}
    return Hypothesis(family=document["name"], rationale="Estrategia definida por el autor en Gherkin; sin optimización.",
                      program=RuleProgram.model_validate(program),
                      **{k: c[k] for k in ("timeframe", "direction", "allocation", "stop_loss", "take_profit", "max_holding")}).model_dump()


def validate_translation(document, program):
    import ast
    p = RuleProgram.model_validate(program).model_dump()
    for rule, key in zip(document["rules"], ("entry", "exit")):
        if "expression" in rule:
            # Normative expressions use literal constants; no hidden parameter substitution.
            if ast.dump(ast.parse(rule["expression"], mode="eval")) != ast.dump(ast.parse(p[key], mode="eval")):
                raise SpecError("CONFORMANCE_FAILED", "La traducción cambia la expresión normativa de " + key)
    return p


def compare(expected, actual, path="resultado"):
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(expected) != set(actual):
            raise AssertionError(path + ": campos diferentes")
        for key in expected:
            compare(expected[key], actual[key], path + "." + key)
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            raise AssertionError(path + ": longitud diferente")
        for i, (left, right) in enumerate(zip(expected, actual)):
            compare(left, right, f"{path}[{i}]")
    elif type(expected) in (int, float):
        if isinstance(actual, (bool, np.bool_)) or not isinstance(actual, Real) or not np.isclose(expected, actual, rtol=0, atol=1e-8):
            raise AssertionError(f"{path}: esperado {expected}, obtenido {actual}")
    elif type(expected) is not type(actual) or expected != actual:
        raise AssertionError(f"{path}: esperado {expected}, obtenido {actual}")


def scenario_frame(scenario, contract):
    if contract["version"] == 2:
        return validate_ticks(pd.DataFrame(scenario["bars"]))
    frame = validate_data(pd.DataFrame(scenario["bars"]), minimum=2)
    seconds = TIMEFRAMES[contract["data_timeframe"]]
    if bar_seconds(frame) != seconds or (frame.timestamp.map(lambda t: t.value) // 10**9 % seconds != 0).any():
        raise SpecError("INVALID_SPEC", "Barras del escenario incompatibles con data_timeframe")
    allowed = {"timestamp", "open", "high", "low", "close", "volume"}
    if set(frame.columns) - allowed:
        raise SpecError("NEEDS_CAPABILITY", "Escenarios: columnas adicionales sin contrato de disponibilidad")
    return frame


def run_scenarios(document, program):
    program = validate_translation(document, program)
    h = hypothesis(document, program)
    cfg = capabilities(Contract.model_validate(document["contract"]))
    results, coverage = [], {"entry": set(), "exit": set()}
    executions = 0
    tick = document["contract"]["version"] == 2
    if tick:
        from tick_engine import tick_signals, replay_ticks
        execution = TickExecution.model_validate(document["contract"]["tick_execution"])
    for scenario in document["scenarios"]:
        frame = scenario_frame(scenario, document["contract"])
        expected = scenario["expected"]
        if scenario["mode"] == "signals":
            if tick:
                en, ex, _ = tick_signals(frame, h, execution)
            else:
                en, ex, _, _ = scheduled_signals(frame, h, signals, program_signals)
            actual = {"entry": en.tolist(), "exit": ex.tolist()}
            if not isinstance(expected, dict) or set(expected) != {"entry", "exit"}:
                raise SpecError("INVALID_SPEC", "Señales: resultado requiere entry y exit completos")
            for key in coverage:
                if not isinstance(expected[key], list) or any(type(v) is not bool for v in expected[key]):
                    raise SpecError("INVALID_SPEC", "Las señales esperadas deben ser booleanos")
                coverage[key].update(expected[key])
        else:
            result = replay_ticks(frame, h, cfg, execution) if tick else backtest(frame, h, cfg, cfg.capital)
            fields = ("entry_timestamp", "exit_timestamp", "direction", "quantity", "entry_price", "exit_price", "pnl")
            if tick:
                fields += ("entry_sequence", "exit_sequence", "exit_reason")
            actual = {"trades": [{k: t[k] for k in fields} for t in result["trade_log"]],
                      "final_equity": result["final_equity"]}
            executions += len(result["trade_log"])
        try:
            compare(expected, actual)
        except AssertionError as exc:
            raise SpecError("CONFORMANCE_FAILED", f"{scenario['name']} (línea {scenario['line']}): {exc}") from exc
        results.append({"name": scenario["name"], "line": scenario["line"], "passed": True, "actual": actual})
    if any(values != {True, False} for values in coverage.values()) or executions == 0:
        raise SpecError("INSUFFICIENT_SCENARIOS", "Se requieren señales verdaderas/falsas de entrada/salida y al menos una operación")
    return results
