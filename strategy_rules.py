"""Composable causal expressions. Model output is parsed as data, never executed."""
import ast
import math

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator


RULE_HELP = """Compose entry and exit expressions freely; no strategy family catalogue.
Use arithmetic + - * /, comparisons > >= < <= == !=, and/or/not, parentheses.
col('field') selects an exact field from the data catalogue; parameters use their names.
Functions: lag(x,n), change(x,n), pct(x,n), sma(x,n), ema(x,n), std(x,n),
lowest(x,n), highest(x,n), total(x,n), rank(x,n), quantile(x,n,q), corr(x,y,n),
abs(x), log(x), sqrt(x), where(condition,a,b). All windows are trailing; lag n>=1.
rank is the percentile of the latest value within its trailing window.
Name every tunable number as a parameter with value, lower, upper, integer.
Use separate entry and exit events. An entry need not remain true while holding.
Signals are observed at the declared signal timeframe close and executed at a subsequent available execution open.
Example: entry="pct(col('close'), horizon) > threshold",
exit="col('close') < sma(col('close'), exit_window)".
Do not use unavailable fields, future shifts, Python code, fitting on the full sample,
or invent data. Missing inputs disable that condition. Cross-market columns are
already delayed to the last completed day; do not assume same-session alignment.
"""


class RuleParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    value: float
    lower: float
    upper: float
    integer: bool = False

    @model_validator(mode="after")
    def bounds(self):
        if not all(math.isfinite(v) for v in (self.value, self.lower, self.upper)):
            raise ValueError("Parámetro no finito")
        if not self.lower <= self.value <= self.upper:
            raise ValueError("Parámetro fuera de sus límites declarados")
        if self.integer and any(v != int(v) for v in (self.value, self.lower, self.upper)):
            raise ValueError("Parámetro entero con límites o valor fraccionarios")
        return self


class RuleProgram(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry: str = Field(min_length=1, max_length=6000)
    exit: str = Field(min_length=1, max_length=6000)
    parameters: list[RuleParameter] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_rules(self):
        parameters = {p.name: p.value for p in self.parameters}
        if len(parameters) != len(self.parameters):
            raise ValueError("Nombres de parámetros repetidos")
        for expr in (self.entry, self.exit):
            inspect_expression(expr, parameters)
        return self


def inspect_expression(expression, parameters):
    """Validate the complete AST before touching a dataset; return fields/lookback."""
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise ValueError("Expresión de reglas inválida") from exc
    if len(list(ast.walk(tree))) > 600:
        raise ValueError("Expresión demasiado compleja para evaluar de forma segura")
    fields = set()
    windows = {"lag": 2, "change": 2, "pct": 2, "sma": 2, "ema": 2,
               "std": 2, "lowest": 2, "highest": 2, "total": 2, "rank": 2,
               "quantile": 3, "corr": 3}

    def number(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.Name) and node.id in parameters:
            return parameters[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -number(node.operand)
        raise ValueError("Ventanas y cuantiles requieren constante o parámetro escalar")

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float, bool):
            if not math.isfinite(node.value):
                raise ValueError("Constante no finita")
            return 0
        if isinstance(node, ast.Name) and node.id in parameters:
            return 0
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            return max(visit(node.left), visit(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Not)):
            return visit(node.operand)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            return max(map(visit, node.values))
        if isinstance(node, ast.Compare) and all(isinstance(op, (ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq)) for op in node.ops):
            return max(map(visit, [node.left, *node.comparators]))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            name, args = node.func.id, node.args
            if name == "col" and len(args) == 1 and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                fields.add(args[0].value)
                return 0
            arity = windows.get(name, {"abs": 1, "log": 1, "sqrt": 1, "where": 3}.get(name))
            if arity is None or len(args) != arity:
                raise ValueError("Función u operación no admitida: " + name)
            lookback = max(map(visit, args))
            if name in windows:
                n = number(args[2] if name == "corr" else args[1])
                if not math.isfinite(n) or n != int(n) or not 1 <= n <= 10000:
                    raise ValueError("Ventana causal requiere entero entre 1 y 10000")
                if name == "quantile" and not 0 <= number(args[2]) <= 1:
                    raise ValueError("Cuantil fuera de [0,1]")
                lookback += int(n)
            return lookback
        raise ValueError("Sintaxis no admitida en reglas: " + type(node).__name__)

    lookback = visit(tree.body)
    return tree, fields, lookback


def program_requirements(program):
    parameters = {p["name"]: p["value"] for p in program["parameters"]}
    inspected = [inspect_expression(program[key], parameters) for key in ("entry", "exit")]
    return set().union(*(row[1] for row in inspected)), max(row[2] for row in inspected)


def validate_tunable_program(program):
    """Research rules must expose every numeric choice to sensitivity analysis.

    Benchmark/export programs may contain constants, so this belongs at research
    admission and the robustness gate, not in the general-purpose interpreter.
    """
    declared = {p["name"]: p for p in program["parameters"]}
    used = set()
    for expression in (program["entry"], program["exit"]):
        tree = ast.parse(expression, mode="eval")
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                raise ValueError("Robustez: todos los números de las señales deben ser parámetros nombrados, incluidos 0 y 1")
            if isinstance(node, ast.Name) and node.id in declared:
                used.add(node.id)
    if not used or used != set(declared):
        raise ValueError("Robustez: se requieren parámetros de señal utilizados, sin parámetros decorativos")
    for name, p in declared.items():
        delta = max(abs(p["value"]) * .2, 1 if p["integer"] else .01)
        # One side may be physically bounded (e.g. lag=1); at least one real
        # perturbation of the declared size must be possible.
        if max(p["value"] - p["lower"], p["upper"] - p["value"]) + 1e-12 < delta:
            raise ValueError("Robustez: rango insuficiente para perturbar " + name)


def evaluate_expression(expression, data, parameters):
    tree, fields, _ = inspect_expression(expression, parameters)
    if fields - set(data.columns):
        raise ValueError("Datos requeridos no disponibles: " + ", ".join(sorted(fields - set(data.columns))))

    def series(value):
        return value if isinstance(value, pd.Series) else pd.Series(value, index=data.index)

    def boolean(value):
        value = series(value)
        if not pd.api.types.is_bool_dtype(value.dtype):
            raise ValueError("Una condición requiere una comparación booleana explícita")
        return value.astype("boolean")

    def run(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return parameters[node.id]
        if isinstance(node, ast.BinOp):
            a, b = run(node.left), run(node.right)
            if isinstance(node.op, ast.Add): return a + b
            if isinstance(node.op, ast.Sub): return a - b
            if isinstance(node.op, ast.Mult): return a * b
            return series(a) / series(b).replace(0, np.nan)
        if isinstance(node, ast.UnaryOp):
            a = run(node.operand)
            if isinstance(node.op, ast.Not): return ~boolean(a)
            return -a if isinstance(node.op, ast.USub) else a
        if isinstance(node, ast.BoolOp):
            values = [boolean(run(v)) for v in node.values]
            result = values[0]
            for value in values[1:]:
                result = result & value if isinstance(node.op, ast.And) else result | value
            return result
        if isinstance(node, ast.Compare):
            a, result = series(run(node.left)), series(True).astype("boolean")
            for op, right in zip(node.ops, node.comparators):
                b = series(run(right))
                if isinstance(op, ast.Gt): value = a > b
                elif isinstance(op, ast.GtE): value = a >= b
                elif isinstance(op, ast.Lt): value = a < b
                elif isinstance(op, ast.LtE): value = a <= b
                elif isinstance(op, ast.Eq): value = a == b
                else: value = a != b
                value = value.astype("boolean").mask(~np.isfinite(a.astype(float)) | ~np.isfinite(b.astype(float)), pd.NA)
                result, a = result & value, b
            return result
        name = node.func.id
        if name == "col": return data[node.args[0].value]
        args = [run(arg) for arg in node.args]
        x = series(args[0])
        if name == "abs": return x.abs()
        if name == "log": return np.log(x.where(x > 0))
        if name == "sqrt": return np.sqrt(x.where(x >= 0))
        if name == "where":
            condition = boolean(args[0])
            return series(args[1]).where(condition.fillna(False), series(args[2])).mask(condition.isna())
        if name == "corr": return x.rolling(int(args[2])).corr(series(args[1]))
        n = int(args[1])
        if name == "lag": return x.shift(n)
        if name == "change": return x - x.shift(n)
        if name == "pct": return x / x.shift(n).replace(0, np.nan) - 1
        if name == "ema": return x.ewm(span=n, min_periods=n, adjust=False).mean()
        window = x.rolling(n, min_periods=n)
        if name == "sma": return window.mean()
        if name == "std": return window.std()
        if name == "lowest": return window.min()
        if name == "highest": return window.max()
        if name == "total": return window.sum()
        if name == "rank": return window.rank(pct=True)
        return window.quantile(args[2])

    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        return boolean(run(tree.body)).fillna(False).to_numpy(dtype=bool)


def program_signals(data, program):
    parameters = {p["name"]: p["value"] for p in program["parameters"]}
    return tuple(evaluate_expression(program[key], data, parameters) for key in ("entry", "exit"))
