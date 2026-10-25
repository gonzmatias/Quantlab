"""Clock-based statistical evaluation of frozen tick rules; never imports an AI client."""
import html
import hashlib
import json
import math
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from experiment_protocol import HoldoutLedger, digest
from gherkin_spec import Contract, capabilities, hypothesis
from research_memory import ResearchMemory
from strategy_rules import program_requirements
from tick_data import RETURN_SECONDS, TickError, TickExecution, load_tick_manifest, utc
from tick_engine import replay_ticks, tick_signals
from trading_agent import deflated_sharpe
from validation import monte_carlo_test, viable


def summary(result):
    return {k: v for k,v in result.items() if k not in ("returns", "equity", "trade_log", "order_log", "return_timestamps")}


def compare_benchmark(result, passive):
    comparison = {"strategy": summary(result), "buy_and_hold": summary(passive)}
    if not passive["trades"]:
        return {**comparison, "passed": False, "status": "INSUFFICIENT_DATA",
                "reason": "El benchmark no pudo ejecutar su entrada con la latencia, caducidad y capital definidos"}
    passed = (result["net_return"] > 0 and result["trades"] > 0 and result["sharpe"] > passive["sharpe"]
              and (result["net_return"] > passive["net_return"] or result["max_drawdown"] < passive["max_drawdown"]))
    return {**comparison, "passed": bool(passed), "status": "PASSED" if passed else "FAILED"}


def causal_ticks(data, h, execution):
    full = tick_signals(data, h, execution)
    checks = []
    for n in sorted({len(data)//2, len(data)*3//4}):
        if n < 2: continue
        prefix = tick_signals(data.iloc[:n], h, execution)
        changed = data.copy()
        changed.loc[changed.index[n:], ["bid", "ask"]] *= 1.731
        mutated = tick_signals(changed, h, execution)
        passed = all(np.array_equal(a[:n], b) and np.array_equal(a[:n], c[:n]) for a,b,c in zip(full,prefix,mutated))
        checks.append({"ticks": n, "passed": bool(passed)})
    return {"passed": bool(checks) and all(c["passed"] for c in checks), "checks": checks}


def tick_walk_forward(data, h, cfg, execution, start, split, end, checkpoint):
    seconds = RETURN_SECONDS[execution.return_interval]
    periods = int((end-split).total_seconds() // seconds)
    cuts = np.linspace(0, periods, cfg.walk_forward_folds+1, dtype=int)
    rows, joined = [], []
    _, _, decision_mask = tick_signals(data, h, execution)
    decision_times = data.timestamp[decision_mask]
    for a,b in zip(cuts[:-1], cuts[1:]):
        checkpoint()
        test_start = split + pd.Timedelta(seconds=int(a)*seconds)
        test_end = split + pd.Timedelta(seconds=int(b)*seconds)
        if execution.holding_unit == "milliseconds":
            train_end = (test_start - pd.Timedelta(milliseconds=h["max_holding"])).floor(f"{seconds}s")
        else:
            preceding = decision_times[decision_times < test_start]
            if len(preceding) < h["max_holding"]:
                return {"passed": False, "status": "INSUFFICIENT_DATA", "reason": "Historia insuficiente para purga por eventos", "folds": rows}
            train_end = preceding.iloc[-h["max_holding"]].floor(f"{seconds}s")
        if train_end-start < pd.Timedelta(seconds=60*seconds) or b-a < 30:
            return {"passed": False, "status": "INSUFFICIENT_DATA", "reason": "Ventanas temporales insuficientes", "folds": rows}
        training = replay_ticks(data, h, cfg, execution, start, train_end, checkpoint=checkpoint)
        testing = replay_ticks(data, h, cfg, execution, test_start, test_end, checkpoint=checkpoint)
        passed = viable(training,cfg) and viable(testing,cfg,max(1,math.ceil(cfg.min_trades/cfg.walk_forward_folds)))
        rows.append({"passed": bool(passed), "train_end": str(train_end), "test_start": str(test_start),
                     "test_end": str(test_end), "training": summary(training), "testing": summary(testing)})
        joined.extend(testing["returns"])
    curve = np.r_[1.,np.cumprod(1+np.array(joined))]
    dd = float(np.max(1-curve/np.maximum.accumulate(curve)))
    passed = (sum(row["passed"] for row in rows) >= math.ceil(2*cfg.walk_forward_folds/3)
              and curve[-1]>1 and dd<=cfg.max_drawdown
              and sum(row["testing"]["trades"] for row in rows)>=cfg.min_trades
              and all(not row["testing"]["bankrupt"] and row["testing"]["max_drawdown"]<=cfg.max_drawdown for row in rows))
    return {"passed": bool(passed), "status": "PASSED" if passed else "FAILED", "folds": rows,
            "net_return": float(curve[-1]-1), "max_drawdown": dd,
            "method": "Ventanas temporales disjuntas, reglas fijas, purga de tenencia; sin optimización"}


def evaluate_ticks(bundle, document, manifest, output, ledger_root, checkpoint=lambda: None):
    from gherkin_pipeline import save_json, mean_interval
    c = Contract.model_validate(document["contract"])
    cfg, h = capabilities(c), hypothesis(document,bundle["program"])
    execution = TickExecution.model_validate(c.tick_execution)
    ticks, metadata = load_tick_manifest(manifest, document["contract"])
    fields, warmup = program_requirements(bundle["program"])
    allowed = {"bid","ask","mid"} if c.timeframe == "tick" else {"open","high","low","close"}
    if fields - allowed:
        raise TickError("NEEDS_DATA", "Campos de señal no disponibles: " + ", ".join(sorted(fields-allowed)))
    start, end = utc(c.evaluation.start), utc(c.evaluation.end)
    seconds = RETURN_SECONDS[execution.return_interval]
    if any(t != t.floor(f"{seconds}s") for t in (start,end)):
        raise TickError("INVALID_SPEC", "Período debe alinearse con la rejilla UTC de retornos")
    ticks = ticks[(ticks.timestamp>=start) & (ticks.timestamp<end)].reset_index(drop=True)
    split = (start+(end-start)*.6).floor(f"{seconds}s")
    holdout_start = (start+(end-start)*.8).floor(f"{seconds}s")
    for a,b,minimum in ((start,split,60),(split,holdout_start,120),(holdout_start,end,120)):
        if (b-a).total_seconds()/seconds < minimum:
            raise TickError("INSUFFICIENT_DATA", "Se requieren 60/120/120 intervalos temporales para entrenamiento/validación/reserva")
    discovery = ticks[ticks.timestamp<holdout_start].copy()
    training_data = discovery[discovery.timestamp<split]
    _, _, decisions = tick_signals(training_data,h,execution)
    if int(decisions.sum()) <= warmup+1:
        raise TickError("INSUFFICIENT_DATA", "Calentamiento de indicadores insuficiente")
    output, ledger_root = Path(output), Path(ledger_root)
    output.mkdir(parents=True,exist_ok=False)
    ledger_root.mkdir(parents=True,exist_ok=True)
    save_json(output/"strategy.json",bundle)
    if metadata.get('storage')=='parquet_duckdb_v1':
        save_json(output/'data_snapshot.json',metadata)
        data_hash=metadata['sha256']
    else:
        ticks.to_csv(output/"ticks.csv",index=False)
        data_hash=hashlib.sha256((output/"ticks.csv").read_bytes()).hexdigest()
    ledger, memory = HoldoutLedger(ledger_root/"holdout_ledger.sqlite3"), ResearchMemory(ledger_root/"research_memory.sqlite3")
    trial = memory.reserve(str(output.resolve()),1,"gherkin:"+str(output.resolve()),bundle["sha256"],digest(bundle["program"]),h,1,limit=None)+1
    report = {"status":"RUNNING","strategy":document["name"],"asset":c.asset,"protocol":"fixed-holdout-v1",
              "execution_model":c.execution_model,"tick_execution":execution.model_dump(),
              "artifact_sha256":bundle["sha256"],"data_sha256":data_hash,
              "metadata":metadata,"settings":asdict(cfg),"trials":trial,"ledger_root":str(ledger_root.resolve()),
              "ai_calls_during_evaluation":0,"conformance":bundle["conformance"],"semantic_scope":bundle["semantic_scope"],
              "scenarios":bundle["scenarios"],"tests":{},"holdout":{"status":"NOT_CONSUMED"},
              "return_interval":execution.return_interval,"dates":{"start":str(start),"validation":str(split),"holdout":str(holdout_start),"end":str(end)},
              "limitations":["Simulación por cruce de cotizaciones: sin cola, profundidad ni fills parciales.",
                             "Los retornos se muestrean por tiempo; no son observaciones independientes por ser intervalos regulares.",
                             "DSR y bootstrap aproximados; evidencia histórica, validación prospectiva pendiente.",
                             "Los escenarios verifican ejemplos, no equivalencia universal de texto libre."]}
    save_json(output/"report.json",report)
    def simulate(a,b,**kwargs):
        return replay_ticks(ticks,h,cfg,execution,a,b,checkpoint=checkpoint,**kwargs)
    def benchmark(a,b,result):
        passive = simulate(a,b,benchmark=True)
        return compare_benchmark(result, passive)
    try:
        checkpoint()
        ledger.observe(start,holdout_start-pd.Timedelta(nanoseconds=1),str(output.resolve()))
        report["tests"]["causality"] = causal_ticks(training_data,h,execution)
        if not report["tests"]["causality"]["passed"]:
            raise TickError("CONFORMANCE_FAILED","Señales cambian con datos futuros")
        ins, oos = simulate(start,split), simulate(split,holdout_start)
        report["in_sample"],report["out_of_sample"] = summary(ins),summary(oos)
        report["tests"]["dsr"] = deflated_sharpe(oos["returns"],cfg,trial,checkpoint)
        report["tests"]["walk_forward"] = tick_walk_forward(discovery,h,cfg,execution,start,split,holdout_start,checkpoint)
        report["tests"]["monte_carlo"] = monte_carlo_test(oos["returns"],cfg,checkpoint)
        report["tests"]["benchmark"] = benchmark(split,holdout_start,oos)
        report["tests"]["costs"] = {}
        for mult in (1,2,3):
            r = oos if mult==1 else simulate(split,holdout_start,cost_multiplier=mult)
            report["tests"]["costs"][str(mult)] = {"passed":bool(viable(r,cfg)),"metrics":summary(r)}
        report["mean_return_interval"] = mean_interval(oos["returns"],cfg.seed,cfg.bootstrap_samples)
        save_json(output/"validation_trades.json",oos["trade_log"])
        save_json(output/"validation_orders.json",oos["order_log"])
        save_json(output/"validation_equity.json",{"timestamps":oos["return_timestamps"],"equity":oos["equity"].tolist(),"returns":oos["returns"].tolist()})
        dsr = report["tests"]["dsr"]
        enough = (min(ins["trades"],oos["trades"])>=cfg.min_trades
                  and report["mean_return_interval"]["status"]=="ESTIMATED"
                  and report["tests"]["benchmark"]["status"]!="INSUFFICIENT_DATA"
                  and report["tests"]["walk_forward"].get("status")!="INSUFFICIENT_DATA")
        passed = (enough and viable(ins,cfg) and viable(oos,cfg) and dsr["dsr"]>=cfg.dsr_probability_min
                  and dsr["dsr_z"]>cfg.dsr_z_min and dsr.get("sequential_passed",False)
                  and all(report["tests"][k]["passed"] for k in ("walk_forward","monte_carlo","benchmark"))
                  and all(v["passed"] for v in report["tests"]["costs"].values()))
        report["status"] = "INSUFFICIENT_EVIDENCE" if not enough else "CRITERIA_NOT_MET"
        if passed:
            final_end = (holdout_start+pd.Timedelta(seconds=max(120*seconds,cfg.forward_min_days*86400))).ceil(f"{seconds}s")
            if final_end>end:
                report["status"],report["holdout"] = "INSUFFICIENT_EVIDENCE",{"status":"INSUFFICIENT_DATA"}
            elif not ledger.reserve(holdout_start,end,str(output.resolve()),bundle["sha256"]):
                report["status"],report["holdout"] = "HOLDOUT_ALREADY_USED",{"status":"ALREADY_USED"}
            else:
                report["holdout"] = {"status":"CONSUMED_PENDING"}
                save_json(output/"report.json",report)
                results = {str(m):simulate(holdout_start,final_end,cost_multiplier=m) for m in (1,2,3)}
                base = results["1"]
                mc = monte_carlo_test(base["returns"],cfg,checkpoint)
                passive = benchmark(holdout_start,final_end,base)
                delayed = simulate(holdout_start,final_end,signal_delay_ms=max(1,execution.latency_ms))
                final_enough = (passive["status"] != "INSUFFICIENT_DATA"
                                and min(r["trades"] for r in (*results.values(), delayed)) >= cfg.final_min_trades)
                final_passed = (final_enough and all(viable(r,cfg,cfg.final_min_trades) for r in results.values())
                                and mc["passed"] and passive["passed"] and viable(delayed,cfg,cfg.final_min_trades))
                report["holdout"] = {"status":("INSUFFICIENT_DATA" if not final_enough else "PASSED" if final_passed else "FAILED"),"passed":bool(final_passed),
                    "start":str(holdout_start),"end":str(final_end),"scenarios":{k:summary(r) for k,r in results.items()},
                    "monte_carlo":mc,"benchmark":passive,"signal_delay":summary(delayed),"extra_signal_delay_ms":max(1,execution.latency_ms)}
                save_json(output/"holdout_trades.json",base["trade_log"])
                save_json(output/"holdout_orders.json",base["order_log"])
                save_json(output/"holdout_equity.json",{"timestamps":base["return_timestamps"],"equity":base["equity"].tolist(),"returns":base["returns"].tolist()})
                report["status"] = "INSUFFICIENT_EVIDENCE" if not final_enough else "HISTORICAL_EVIDENCE" if final_passed else "CRITERIA_NOT_MET"
        return report
    except Exception as exc:
        report["status"],report["error"] = getattr(exc,"status","INTERRUPTED"),str(exc)
        raise
    finally:
        save_json(output/"report.json",report)
        formatted = json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)
        (output/"report.md").write_text("# Evaluación tick Gherkin\n\n```json\n"+formatted+"\n```\n",encoding="utf-8")
        (output/"report.html").write_text("<!doctype html><meta charset='utf-8'><h1>Evaluación tick Gherkin</h1><pre style='white-space:pre-wrap'>"+html.escape(formatted)+"</pre>",encoding="utf-8")
        memory.save(str(output.resolve()),1,report,report["status"])
        memory.export_audit(output/"research_audit.json")
