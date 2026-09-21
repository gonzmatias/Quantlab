"""Agente de reportes basado exclusivamente en resultados calculados.

No delega la aprobación a un LLM ni inventa métricas ausentes.
Cada intento produce JSON, Markdown y HTML independiente e imprimible.
"""
from __future__ import annotations

import html
import json
from research import safe_source_url
from validation import validation_complete, VALIDATION_POLICY
from datetime import datetime, timezone
from pathlib import Path


FAMILY_NAMES = {
    "trend": "Seguimiento de tendencia",
    "mean_reversion": "Reversión a la media",
    "breakout": "Ruptura de máximos",
    "momentum": "Momentum de precios",
    "dip": "Compra de retrocesos",
    "ema_trend": "Tendencia con medias exponenciales",
    "rsi_reversion": "Reversión de sobreventa RSI",
    "volatility_breakout": "Ruptura ajustada por volatilidad",
}


def strategy_name(hypothesis: dict) -> str:
    if not hypothesis:
        return "Hipótesis no disponible"
    family = hypothesis.get("family", "")
    if hypothesis.get("program"):
        return family + " · reglas compuestas"
    return f"{FAMILY_NAMES.get(family, family)} · {hypothesis.get('fast', '?')}/{hypothesis.get('slow', '?')}"


def number(value, percent: bool = False) -> str:
    if value is None:
        return "No evaluado"
    return f"{value * 100:.2f}%" if percent else f"{value:.2f}"


def assessment(quant, stress):
    oos = quant.get("out_of_sample", {})
    profitability = "NO EVALUADA"
    if "net_return" in oos:
        profitability = "POSITIVA EN OOS" if oos["net_return"] > 0 and oos.get("trades", 0) > 0 else "SIN GANANCIA OOS OPERABLE"
    robust = "APROBADA" if validation_complete(quant, stress) else "NO APROBADA" if quant else "NO EVALUADA"
    return {"historical_profitability": profitability, "robustness": robust,
            "independent_validation": "PENDIENTE", "basis": "Retorno neto OOS con operaciones; no predicción de rentabilidad futura"}


def build_report(state: dict, settings: dict, synthetic: bool) -> dict:
    quant, stress = state.get("quant_metrics", {}), state.get("stress_metrics", {})
    ins, oos = quant.get("in_sample", {}), quant.get("out_of_sample", {})
    approved = state.get("status") == "APPROVED" and validation_complete(quant, stress) and not synthetic
    reasons = list(quant.get("rejection_reasons", [])) + list(stress.get("rejection_reasons", []))
    if not approved and not reasons:
        reasons = state.get("logs", [])[-1:] or ["No se completaron todas las pruebas."]
    checks = []

    def check(label, value, requirement, passed):
        checks.append({"label": label, "value": value, "requirement": requirement, "passed": passed})

    if quant:
        check("DSR probabilístico", number(quant.get("dsr"), True),
              f"≥ {settings['dsr_probability_min']:.0%}", quant.get("dsr", 0) >= settings["dsr_probability_min"])
        check("Estadístico z del DSR", number(quant.get("dsr_z")),
              f"> {settings['dsr_z_min']}", quant.get("dsr_z", 0) > settings["dsr_z_min"])
        check("Evidencia secuencial", number(quant.get("dsr_z")),
              f"z ≥ {number(quant.get('sequential_z_min'))}; ajuste por intento y búsqueda acumulada",
              quant.get("sequential_passed", False))
        check("Degradación de Sharpe OOS", number(quant.get("oos_degradation"), True),
              "≤ 20%; Sharpe IS y OOS positivos", quant.get("oos_degradation", 1) <= .2
              and ins.get("sharpe", 0) > 0 and oos.get("sharpe", 0) > 0)
        for name, result in (("IS", ins), ("OOS", oos)):
            check(f"Sharpe {name}", number(result.get("sharpe")), "> 0",
                  result.get("sharpe", 0) > 0)
            check(f"Operaciones {name}", str(result.get("trades", 0)), f"≥ {settings['min_trades']}",
                  result.get("trades", 0) >= settings["min_trades"])
            check(f"Drawdown {name}", number(result.get("max_drawdown"), True),
                  f"≤ {settings['max_drawdown']:.0%}", result.get("max_drawdown", 1) <= settings["max_drawdown"])
            check(f"Retorno neto {name}", number(result.get("net_return"), True), "> 0%",
                  result.get("net_return", 0) > 0)
        pf_ok = bool(oos.get("no_losing_trades") or (oos.get("profit_factor") or 0) >= 1.2)
        check("Profit Factor OOS", "Sin pérdidas realizadas" if oos.get("no_losing_trades") else number(oos.get("profit_factor")),
              "≥ 1.20", pf_ok)
        positive = sum(f.get("net_return", 0) > 0 for f in quant.get("forward_windows", []))
        check("Ventanas progresivas positivas", f"{positive}/3", "≥ 2/3", positive >= 2)
    else:
        check("Validación cuantitativa", "No ejecutada", "Requiere hipótesis válida", None)
    for multiplier, scenario in stress.get("scenarios", {}).items():
        passed = (not scenario.get("bankrupt", True) and scenario.get("net_return", 0) > 0
                  and scenario.get("max_drawdown", 1) <= settings["max_drawdown"]
                  and scenario.get("trades", 0) >= settings["min_trades"])
        check(f"Estrés $100 · costos ×{multiplier}",
              f"${scenario.get('final_equity', 0):.2f} · DD {number(scenario.get('max_drawdown'), True)} · {scenario.get('trades', 0)} operaciones",
              f"Ganancia; DD ≤ {settings['max_drawdown']:.0%}; ≥ {settings['min_trades']} operaciones; sin quiebra", passed)
    if not stress:
        check("Estrés de $100", "No ejecutado", "Requiere aprobar validación cuantitativa", None)
    advanced_specs = {
        "regression": ("Regresión histórica alfa/beta", "Límite inferior del alfa HAC > 0; al menos 60 observaciones"),
        "walk_forward": ("Walk-forward cronológico", "≥ 2/3 ventanas válidas; retorno agregado positivo, operaciones y DD dentro de límites"),
        "parameters": ("Estrés de parámetros ±20%", f"≥ {settings.get('parameter_pass_min', .8):.0%} de variantes válidas"),
        "monte_carlo": ("Monte Carlo por bloques", f"P(ganancia) ≥ {settings.get('monte_carlo_profit_min', .9):.0%}; DD percentil 95 dentro del límite; ruina ≤ 1%"),
        "signal_delay": ("Retraso de señales", "Un día adicional; retorno y Sharpe positivos, operaciones y DD dentro de límites"),
        "concentration": ("Dependencia de días excepcionales", "Retorno positivo al anular los cinco mejores días"),
    }
    tests = {**quant.get("advanced_tests", {}), **stress.get("robustness_tests", {})}
    for key, (label, requirement) in advanced_specs.items():
        result = tests.get(key, {})
        evaluated = result and result.get("status") != "SKIPPED"
        check(label, result.get("reason", "No ejecutada; requiere aprobar filtros anteriores"), requirement,
              bool(result.get("passed")) if evaluated else None)
        checks[-1]["required"] = key in VALIDATION_POLICY["mandatory"]
        if not checks[-1]["required"]:
            checks[-1]["label"] += " · diagnóstico"
    outcome = "APPROVED" if approved else "REJECTED"
    attempt = state.get("iteration_count", 0)
    return {
        "schema_version": 3, "attempt": attempt, "created_at": datetime.now(timezone.utc).isoformat(),
        "validation_policy": VALIDATION_POLICY,
        "assessment": assessment(quant, stress),
        "asset_assessments": {a: assessment(r.get("quant_metrics", {}), r.get("stress_metrics", {})) for a, r in state.get("asset_results", {}).items()},
        "research": state.get("research", {}),
        "strategy_name": state.get("strategy_name") or strategy_name(state.get("hypothesis", {})),
        "outcome": outcome, "synthetic": synthetic, "hypothesis": state.get("hypothesis", {}),
        "mql5_export": state.get("mql5_export", {}),
        "selected_asset": state.get("selected_asset", ""),
        "asset_results": {a: {k: v for k, v in r.items() if k != "charts"} for a, r in state.get("asset_results", {}).items()},
        "positive_assets": [a for a, r in state.get("asset_results", {}).items() if r.get("quant_metrics", {}).get("out_of_sample", {}).get("net_return", 0) > 0],
        "ranking_rule": "Cada activo debe superar backtest, walk-forward, costos, parámetros, Monte Carlo y retraso. Regresión y concentración son diagnósticos. Entre aprobados: mayor Sharpe OOS, menor drawdown y mayor retorno.",
        "summary": ((f"La estrategia superó los filtros cuantitativos y de capital reducido en {state.get('selected_asset') or 'el activo evaluado'}."
                    if approved else "El intento no superó todos los filtros. Se conserva el diagnóstico para la siguiente hipótesis.")
                    + (" Comparación completada: " + ", ".join(state["asset_results"]) + "." if state.get("asset_results") else "")),
        "next_action": ("Revisar el reporte y validar con datos independientes." if approved else
                        "Límite de intentos alcanzado: búsqueda finalizada." if settings["max_iterations"] > 0 and attempt >= settings["max_iterations"] else
                        "Volver a investigación con los motivos de rechazo."),
        "checks": checks, "rejection_reasons": reasons, "quant_metrics": quant,
        "stress_metrics": stress, "charts": state.get("charts", {}), "settings": settings,
        "limitations": ["Datos sintéticos de demostración; no prueban alfa." if synthetic else
                        "OOS reutilizado de forma adaptativa; requiere una prueba independiente.",
                        "DSR aproximado mediante bootstrap por bloques.",
                        "Regresión explicativa de retornos con alfa/beta y errores HAC; no predice precios ni prueba causalidad. Tipo libre de riesgo supuesto cero.",
                        "Walk-forward con entrenamiento creciente, separación max_holding y reglas congeladas; no reoptimiza parámetros.",
                        "Monte Carlo remuestrea retornos netos históricos por bloques. No reconstruye ejecuciones ni eventos nunca observados; ruina operativa = pérdida del 95% del capital inicial.",
                        "Los umbrales de robustez son criterios configurados, no garantías estadísticas de rentabilidad futura. Una prueba fallida omite las posteriores de ese activo.",
                        "La evidencia exigida aumenta con los intentos; el ajuste secuencial no valida por sí solo un OOS adaptativo.",
                        "Stops al cierre y ejecución en apertura siguiente; el drawdown es al cierre.",
                        "Costos y mínimos por activo son supuestos editables, no tarifas verificadas. FX/oro incluyen débito diario supuesto; sin apalancamiento ni créditos de swap.",
                        "Cada combinación estrategia-activo cuenta en el ajuste por múltiples pruebas. La clasificación depende de estos cinco activos, este período y estos costos.",
                        "Dukascopy: OHLC BID; CAD/USD invierte USD/CAD (referencia ASK). BTC: operaciones spot Coinbase. Los extremos diarios no reproducen el libro de órdenes.",
                        "El simulador Python no envía órdenes. El EA MQL5 puede operar al habilitar EnableTrading; compilación y validación MT5 pendientes."],
    }


def svg_chart(series: dict) -> str:
    values = series.get("equity", [])
    if len(values) < 2:
        return "<p>No hay una curva disponible para esta prueba.</p>"
    low, high = min(values), max(values)
    span = high - low or max(abs(high) * .02, 1)
    points = " ".join(f"{40+i*920/(len(values)-1):.2f},{210-(v-low)*160/span:.2f}" for i, v in enumerate(values))
    return (f'<svg viewBox="0 0 1000 250" role="img" aria-label="Curva de capital">'
            f'<text x="40" y="24">{html.escape(series.get("label", "Capital"))} · USD</text>'
            f'<path d="M40 215H970" stroke="#dce3e9"/>'
            f'<polyline points="{points}" fill="none" stroke="#138c78" stroke-width="3"/>'
            f'<text x="40" y="244">Inicio ${values[0]:,.2f}</text>'
            f'<text x="750" y="244">Final ${values[-1]:,.2f}</text></svg>')


def render_html(report: dict) -> str:
    esc = lambda value: html.escape(str(value))
    rows = "".join(f"<tr><td>{esc(c['label'])}</td><td>{esc(c['value'])}</td>"
                   f"<td>{esc(c['requirement'])}</td><td>{'Cumple' if c['passed'] is True else 'No cumple' if c['passed'] is False else 'No evaluado'}</td></tr>"
                   for c in report["checks"])
    reasons = "".join(f"<li>{esc(reason)}</li>" for reason in report["rejection_reasons"])
    charts = "".join(svg_chart(series) for series in report["charts"].values())
    limits = "".join(f"<li>{esc(item)}</li>" for item in report["limitations"])
    h = report["hypothesis"]
    assets = asset_table_html(report)
    verdicts = report.get("assessment", {})
    return f'''<!doctype html><html lang="es"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reporte · {esc(report['strategy_name'])}</title>
<style>body{{font:16px system-ui;color:#182b39;background:#f3f6f8;margin:0;padding:32px}}
main{{max-width:1060px;margin:auto;background:white;padding:40px;border-radius:18px}}
h1{{font-size:32px}}h2{{margin-top:32px}}p,li{{line-height:1.6}}small{{color:#536c7b}}
.badge{{background:#e7f4ef;padding:8px 12px;border-radius:8px;display:inline-block}}
table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{padding:12px;text-align:left;border-bottom:1px solid #dce3e9}}
svg{{width:100%;margin:12px 0}}svg text{{font:13px system-ui;fill:#536c7b}}
pre{{white-space:pre-wrap;background:#f3f6f8;padding:16px}}@media print{{body,main{{padding:0;background:white}}tr,svg{{break-inside:avoid}}}}</style>
<main><small>QUANT LAB / REPORTE DE PRUEBAS / INTENTO {report['attempt']}</small>
<h1>{esc(report['strategy_name'])}</h1><span class="badge">{'APROBADO EN SIMULACIÓN' if report['outcome']=='APPROVED' else 'RECHAZADO'}</span>
<p>{esc(report['summary'])}</p><small>{esc(report['created_at'])} · {'Demo sintética' if report['synthetic'] else 'Datos públicos'}</small>
<p><b>Rentabilidad histórica:</b> {esc(verdicts.get('historical_profitability', 'NO EVALUADA'))} · <b>Robustez:</b> {esc(verdicts.get('robustness', 'NO EVALUADA'))} · <b>Validación independiente:</b> PENDIENTE</p>
<h2>Comparación por activo</h2>{assets}
<h2>Hipótesis y parámetros</h2><p>{esc(h.get('rationale', 'No se obtuvo una hipótesis válida.'))}</p>
<pre>{esc(json.dumps(h, ensure_ascii=False, indent=2))}</pre>
{research_html(report)}
<h2>Exportación MQL5</h2><pre>{esc(json.dumps(report.get("mql5_export", {}), ensure_ascii=False, indent=2))}</pre>
<h2>Resultados frente a los filtros</h2><table><thead><tr><th>Prueba</th><th>Resultado</th><th>Requisito</th><th>Estado</th></tr></thead><tbody>{rows}</tbody></table>
<details><summary>Detalle de regresión, walk-forward y robustez</summary><pre>{esc(json.dumps({'quant': report['quant_metrics'].get('advanced_tests', {}), 'stress': report['stress_metrics'].get('robustness_tests', {})}, ensure_ascii=False, indent=2))}</pre></details>
<h2>Capital durante las pruebas</h2>{charts or '<p>No se ejecutaron pruebas con curva de capital.</p>'}
<h2>Diagnóstico y siguiente paso</h2><ul>{reasons}</ul><p>{esc(report['next_action'])}</p>
<h2>Alcance de la evidencia</h2><ul>{limits}</ul></main></html>'''


def write_report(report: dict, output: Path) -> None:
    folder = output / "reports"
    folder.mkdir(exist_ok=True)
    prefix = folder / f"attempt_{report['attempt']:02}"
    prefix.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    prefix.with_suffix(".html").write_text(render_html(report), encoding="utf-8")
    lines = [f"# {report['strategy_name']}", "", f"**{report['outcome']} · Intento {report['attempt']}**", "",
             report["summary"], "", "## Pruebas", ""]
    lines += [f"- {key}: {value}" for key, value in report.get("assessment", {}).items()]
    lines += [f"- {c['label']}: {c['value']} (requisito: {c['requirement']}). "
              + ("Cumple." if c["passed"] is True else "No cumple." if c["passed"] is False else "No evaluado.") for c in report["checks"]]
    lines += ["", "## Diagnóstico", ""] + [f"- {r}" for r in report["rejection_reasons"]]
    if report.get("mql5_export"):
        lines += ["", "## Exportación MQL5", "", "```json", json.dumps(report["mql5_export"], ensure_ascii=False, indent=2), "```"]
    lines += ["", report["next_action"], "", "## Limitaciones", ""] + [f"- {r}" for r in report["limitations"]]
    lines += ["", "## Investigación y procedencia", "", "```json",
              json.dumps(report.get("research", {}), ensure_ascii=False, indent=2), "```",
              "", "## Comparación de referencia en entrenamiento", "", "```json",
              json.dumps(report.get("quant_metrics", {}).get("baseline_comparison", {}), ensure_ascii=False, indent=2), "```"]
    lines += ["", "## Pruebas avanzadas", "", "```json", json.dumps({
        "quant": report.get("quant_metrics", {}).get("advanced_tests", {}),
        "stress": report.get("stress_metrics", {}).get("robustness_tests", {})}, ensure_ascii=False, indent=2), "```"]
    lines += ["", "## Resultados por activo", "", f"Activo seleccionado: {report.get('selected_asset', '')}", ""]
    for asset, row in report.get("asset_results", {}).items():
        lines += [f"### {asset}", "", "```json", json.dumps(row, ensure_ascii=False, indent=2), "```", ""]
    prefix.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")


def research_html(report):
    esc = lambda value: html.escape(str(value))
    research = report.get("research", {})
    brief = research.get("brief", {})
    if not brief:
        return "<h2>Investigación</h2><p>" + esc(research.get("summary", research.get("rejection_reason", "Sin ficha de investigación."))) + "</p>"
    labels = {"mechanism": "Mecanismo", "prediction": "Predicción", "falsification": "Criterio de descarte",
              "adaptation": "Adaptación", "parameter_reasoning": "Justificación de parámetros",
              "compatibility_reason": "Compatibilidad", "research_approach": "Enfoque de investigación",
              "change_from_previous": "Cambio frente a intentos anteriores", "contrary_evidence": "Evidencia contraria",
              "data_requests": "Datos pendientes", "data_acquisition": "Fuentes de datos solicitadas"}
    parts = ["<h2>Investigación y fuentes</h2>"]
    parts += [f"<p><b>{label}:</b> {esc(brief.get(key, ''))}</p>" for key, label in labels.items()]
    for source in brief.get("sources", []):
        url = source.get("url", "")
        title = esc(source.get("title", url))
        link = f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{title}</a>' if safe_source_url(url) else title
        parts.append(f"<p>{link} · {esc(source.get('source_kind', ''))}<br>{esc(source.get('finding', ''))}<br>Limitaciones: {esc(source.get('limitations', ''))}</p>")
    parts.append("<p>La trazabilidad de la URL no verifica por sí sola las afirmaciones de la fuente. La predicción del mecanismo requiere revisión; los filtros validan rendimiento simulado.</p>")
    parts.append("<h3>Comparación de referencia en entrenamiento</h3><pre>" + esc(json.dumps(report.get("quant_metrics", {}).get("baseline_comparison", {}), ensure_ascii=False, indent=2)) + "</pre>")
    return "".join(parts)


def asset_table_html(report):
    esc = lambda x: html.escape(str(x))
    rows = []
    for asset, r in report.get("asset_results", {}).items():
        q, s = r["quant_metrics"], r.get("stress_metrics", {})
        o, ins = q.get("out_of_sample", {}), q.get("in_sample", {})
        verdict = "APROBADO" if q.get("passed") and s.get("passed") and not report["synthetic"] else "POSITIVO · filtros pendientes" if o.get("net_return", 0) > 0 else "RECHAZADO"
        reasons = q.get("rejection_reasons", []) + s.get("rejection_reasons", [])
        rows.append(f"<tr><td><b>{esc(asset)}</b><br>{esc(r.get('metadata', {}).get('source', 'Demo'))}</td>"
                    f"<td>{number(ins.get('net_return'), True)}</td><td>{number(o.get('net_return'), True)}</td>"
                    f"<td>{number(o.get('sharpe'))}</td><td>{number(q.get('dsr'), True)}</td>"
                    f"<td>{number(o.get('max_drawdown'), True)}</td><td>{o.get('trades', 0)}</td>"
                    f"<td>{esc(verdict)}<br>{esc('; '.join(reasons))}</td></tr>")
    details = "".join(f"<details><summary>{esc(a)} · procedencia, costos y pruebas completas</summary><pre>{esc(json.dumps(r, ensure_ascii=False, indent=2))}</pre></details>" for a,r in report.get("asset_results", {}).items())
    return (f"<p>Activo destacado: <b>{esc(report.get('selected_asset', ''))}</b>. "
            f"OOS positivo: {esc(', '.join(report.get('positive_assets', [])) or 'ninguno')}. Ganancia histórica no implica aprobación.</p>"
            f"<p>{esc(report.get('ranking_rule', ''))}</p><table><thead><tr><th>Activo</th><th>IS</th><th>OOS</th><th>Sharpe</th><th>DSR</th><th>DD</th><th>Operaciones</th><th>Resultado</th></tr></thead><tbody>{''.join(rows)}</tbody></table>{details}")
