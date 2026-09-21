"""Deterministic MQL5 EA export from the rules actually tested by LangGraph."""
from __future__ import annotations

import ast
import hashlib
import json
import re
import zipfile
from pathlib import Path

import pandas as pd

from strategy_rules import inspect_expression, program_requirements, program_signals


def executable_program(h):
    """Legacy rules use the same operators as composed rules, without an LLM rewrite."""
    if h.get("program"):
        return h["program"], False
    c, fast, slow, threshold = "col('close')", h["fast"], h["slow"], h["threshold"]
    sma_fast, sma_slow = f"sma({c},{fast})", f"sma({c},{slow})"
    delta = f"change({c},1)"
    gains, losses = f"sma(where({delta}>=0,{delta},0),{fast})", f"sma(where({delta}<0,-({delta}),0),{fast})"
    rules = {
        "trend": f"{sma_fast}>{sma_slow}",
        "mean_reversion": f"({c}-{sma_slow})/std({c},{slow}) < -{threshold}",
        "breakout": f"{c}>lag(highest(col('high'),{slow}),1)",
        "momentum": f"pct({c},{slow})>{threshold}/100",
        "dip": f"({c}>{sma_slow}) and ({c}<{sma_fast})",
        "ema_trend": f"ema({c},{fast})>ema({c},{slow})",
        "rsi_reversion": f"(100*{gains}/({gains}+{losses}) < 50-10*{threshold}) and ({c}>{sma_slow})",
        "volatility_breakout": f"{c}>{sma_slow}+{threshold}*std({c},{slow})",
    }
    if h["family"] not in rules:
        raise ValueError("No hay reglas ejecutables para exportar")
    entry = rules[h["family"]]
    if h.get("regime") == "uptrend":
        entry = f"({entry}) and ({c}>{sma_slow})"
    elif h.get("regime") == "low_volatility":
        entry = f"({entry}) and (lag(std(pct({c},1),{fast}),1)<lag(std(pct({c},1),{slow}),1))"
    if h.get("confirmation") == "strong_close":
        entry = f"({entry}) and (({c}-col('low'))/(col('high')-col('low'))>=0.75)"
    return {"entry": entry, "exit": f"not ({entry})", "parameters": []}, True


def compile_program(program):
    """Compile the validated AST into a topologically ordered numerical graph."""
    parameters = {p["name"]: p["value"] for p in program["parameters"]}
    nodes, cache = [], {}

    def emit(op, args=(), value=0., field=""):
        key = (op, tuple(args), value, field)
        if key not in cache:
            cache[key] = len(nodes)
            nodes.append({"op": op, "args": list(args), "value": value, "field": field})
        return cache[key]

    def visit(node):
        if isinstance(node, ast.Constant):
            return emit("constant", value=float(node.value))
        if isinstance(node, ast.Name):
            return emit("constant", value=float(parameters[node.id]))
        if isinstance(node, ast.UnaryOp):
            op = {ast.Not: "not", ast.USub: "neg", ast.UAdd: "positive"}[type(node.op)]
            return emit(op, [visit(node.operand)])
        if isinstance(node, ast.BinOp):
            op = {ast.Add: "add", ast.Sub: "sub", ast.Mult: "mul", ast.Div: "div"}[type(node.op)]
            return emit(op, [visit(node.left), visit(node.right)])
        if isinstance(node, ast.BoolOp):
            args = [visit(v) for v in node.values]
            result = args[0]
            for arg in args[1:]:
                result = emit("and" if isinstance(node.op, ast.And) else "or", [result, arg])
            return result
        if isinstance(node, ast.Compare):
            left, result = visit(node.left), None
            for op, right in zip(node.ops, node.comparators):
                right = visit(right)
                name = {ast.Gt: "gt", ast.GtE: "ge", ast.Lt: "lt", ast.LtE: "le", ast.Eq: "eq", ast.NotEq: "ne"}[type(op)]
                current = emit(name, [left, right])
                result = current if result is None else emit("and", [result, current])
                left = right
            return result
        if node.func.id == "col":
            return emit("field", field=node.args[0].value)
        return emit(node.func.id, [visit(arg) for arg in node.args])

    roots = [visit(inspect_expression(program[key], parameters)[0].body) for key in ("entry", "exit")]
    return nodes, roots


def mql_string(value):
    # JSON string escaping matches the string literals used here; never interpolate comments/code.
    return json.dumps(str(value), ensure_ascii=True)


def ema_span3_gap_correction():
    """Freeze the active pandas engine's span=3 missing-value behavior in the EA."""
    probe = pd.Series([1., float("nan"), 4.]).ewm(span=3, adjust=False).mean().iloc[-1]
    if abs(probe - 3.25) < 1e-12:
        return True
    if abs(probe - 3.) < 1e-12:
        return False
    raise ValueError("Semántica EMA de pandas desconocida: requiere revisar la exportación")


def generate_source(h, asset, start, feature_file, signature):
    if not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise ValueError("Firma de hipótesis inválida")
    program, legacy = executable_program(h)
    fields, warmup = program_requirements(program)
    nodes, roots = compile_program(program)
    native = {"open", "high", "low", "close", "weekday", "month", "day"}
    extras = sorted(fields - native)
    setup = []
    for index, node in enumerate(nodes):
        args = (node["args"] + [-1] * 3)[:3]
        setup.append(f"   SetNode({index},{mql_string(node['op'])},{args[0]},{args[1]},{args[2]},{node['value']:.17g},{mql_string(node['field'])});")
    replacements = {
        "@@SYMBOL@@": mql_string(asset), "@@START@@": str(int(start.timestamp())),
        "@@NODES@@": str(len(nodes)), "@@SETUP@@": "\n".join(setup),
        "@@ENTRY@@": str(roots[0]), "@@EXIT@@": str(roots[1]),
        "@@LEGACY@@": "true" if legacy else "false", "@@WARMUP@@": str(warmup),
        "@@FEATURE_FILE@@": mql_string(feature_file), "@@FEATURE_COUNT@@": str(len(extras)),
        "@@FEATURE_SETUP@@": "\n".join(f"   feature_names[{i}]={mql_string(field)};" for i, field in enumerate(extras)),
        "@@ALLOCATION@@": repr(float(h["allocation"])), "@@STOP@@": repr(float(h["stop_loss"])),
        "@@TAKE@@": repr(float(h["take_profit"])), "@@HOLDING@@": str(h["max_holding"]),
        "@@MAGIC@@": str(100000 + int(signature[:8], 16) % 2000000000),
        "@@SIGNATURE@@": signature,
        "@@SPAN3_GAP@@": "true" if ema_span3_gap_correction() else "false",
    }
    source = Path(__file__).with_name("mql5_runtime.mq5.tmpl").read_text(encoding="utf-8")
    for token, value in replacements.items():
        source = source.replace(token, value)
    if "@@" in source:
        raise ValueError("Plantilla MQL5 incompleta")
    return source, program, extras, legacy


def export_mql5(h, asset, data, folder, signature, settings, checkpoint=lambda: None):
    """Write a self-contained source bundle. Compilation and MT5 parity remain explicit."""
    if not re.fullmatch(r"[A-Za-z0-9_]+", asset):
        raise ValueError("Activo inválido para exportar MQL5")
    checkpoint()
    feature_name = f"quantlab_{signature[:12]}_features.csv"
    source, program, extras, legacy = generate_source(h, asset, data.timestamp.iloc[0], feature_name, signature)
    source = source.replace("input double StrategyCapitalUSD=100.0;", f"input double StrategyCapitalUSD={float(settings.get('capital', 10000)):.8f};")
    missing = set(extras) - set(data.columns)
    if missing:
        raise ValueError("Faltan datos de la estrategia aprobada: " + ", ".join(sorted(missing)))
    entry, exit_signal = program_signals(data, program)
    if legacy:
        exit_signal = ~entry
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    source_path = folder / "strategy.mq5"
    source_path.write_text(source, encoding="utf-8")
    files = [source_path]
    if extras:
        features = data[extras].copy()
        features.insert(0, "timestamp_utc", data.timestamp.map(lambda t: int(t.timestamp())))
        path = folder / feature_name
        features.to_csv(path, sep=";", index=False, na_rep="", float_format="%.17g")
        files.append(path)
    reference = data[["timestamp"]].copy()
    reference["entry"] = entry.astype(int)
    reference["exit"] = exit_signal.astype(int)
    reference.to_csv(folder / "reference_signals.csv", index=False)
    files.append(folder / "reference_signals.csv")
    manifest = {"format": "mql5-ea", "version": 1, "asset": asset, "hypothesis_signature": signature,
                "pandas_version": pd.__version__, "ema_span3_gap_correction": ema_span3_gap_correction(),
                "hypothesis": h, "executable_program": program, "legacy_exit_complement": legacy,
                "simulation_settings": settings, "required_feature_fields": extras,
                "feature_file": feature_name if extras else None,
                "data_start": str(data.timestamp.iloc[0]), "data_end": str(data.timestamp.iloc[-1]),
                "data_sha256": hashlib.sha256(data.to_csv(index=False).encode()).hexdigest(),
                "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                "compilation": "PENDING_METAEDITOR", "mt5_parity": "PENDING_STRATEGY_TESTER",
                "independent_validation": "PENDING", "live_trading_default": False,
                "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    files.append(folder / "manifest.json")
    instructions = f"""# Expert Advisor: {asset}

Reglas exportadas de la hipótesis {signature}; no se reformulan mediante un LLM.
La aprobación corresponde al simulador Python. Compilación MQL5, paridad en MT5 y
validación con datos independientes están PENDIENTES, no certificadas.

1. Copiar strategy.mq5 a MQL5/Experts y compilar en MetaEditor (F7).
2. Probar en Strategy Tester sobre D1. BrokerSymbol debe representar {asset} con
   cotización USD y la misma orientación. CADUSD NO equivale a comprar USDCAD.
3. EnableTrading=false deja el EA en modo señales. Habilitarlo sólo al querer
   ejecutar órdenes en Tester/demo/terminal. La generación no conecta a un bróker.
4. Si existe {feature_name}, copiarlo a Terminal/Common/Files. El EA requiere una
   fila de fecha UTC exacta por vela para los campos: {', '.join(extras) or 'ninguno'}.
   Volumen, datos externos y mercados cruzados usan este archivo, sin reemplazos
   por tick volume ni cotizaciones distintas. Actualizarlo con el mismo proceso
   causal para evaluar fechas nuevas; este bundle sólo incluye el snapshot histórico.
5. BrokerUtcOffsetMinutes relaciona las fechas de velas con el CSV, pero no convierte
   sesiones distintas ni resuelve cambios históricos de horario/DST. Comparar datos,
   calendario, aperturas, moneda, contratos, spread, swaps y comisiones del bróker.
6. EmitSignalLog escribe fechas UTC y señales en el diario para contrastarlas con
   reference_signals.csv usando exactamente las mismas velas y datos auxiliares.

Se evalúan velas cerradas y se actúa al primer tick de la nueva vela D1. Al adjuntar
el EA se espera la siguiente vela, sin recuperar entradas antiguas. Las entradas
tardías se omiten. Stop/objetivo se evalúan al cierre, no como órdenes intrabar.
Se conserva una posición larga por símbolo/magic; posiciones ajenas y órdenes pendientes bloquean acciones.
StrategyCapitalUSD es el capital inicial, actualizado con P&L, comisiones, swaps y
fees del historial de ese símbolo/magic. Evitar reutilizar el magic para operaciones
ajenas o cambiar el capital inicial después de empezar. El tamaño queda limitado
por ese saldo, efectivo disponible y nocional sin leverage;
pasos de lote, margen y mínimo del bróker pueden impedir operar. El valor inicial de
StrategyCapitalUSD es {float(settings.get('capital', 10000)):,.2f}, coherente con el capital validado. Comisiones, swaps y precios
reales no se reconstruyen con los supuestos del simulador. Revalidar cambios de inputs.
El EA no fuerza la liquidación al finalizar un histórico: el tester debe contemplar
la posición residual al comparar el cierre forzado del simulador Python.
"""
    (folder / "README.md").write_text(instructions, encoding="utf-8")
    files.append(folder / "README.md")
    checkpoint()
    archive = folder / "strategy_bundle.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            bundle.write(path, path.name)
    return {"status": "EXPORTED", "directory": str(folder.resolve()), "source": str(source_path.resolve()),
            "bundle": str(archive.resolve()), "manifest": str((folder / "manifest.json").resolve()),
            "source_sha256": manifest["source_sha256"], "compilation": manifest["compilation"],
            "mt5_parity": manifest["mt5_parity"], "required_feature_fields": extras}
