import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from mql5_export import compile_program, executable_program, export_mql5, generate_source, mql_string, ema_span3_gap_correction
from strategy_rules import program_signals
from trading_agent import (TradingAgent, Settings, initial_state, demo_data, variant_hypothesis,
                           hypothesis_signature, signals)
from test_strategy_rules import hypothesis


class Mql5Tests(unittest.TestCase):
    def test_legacy_translation_matches_original_signals(self):
        data = demo_data()
        for index in range(8):
            for regime in ("none", "uptrend", "low_volatility"):
                for confirmation in ("none", "strong_close"):
                    h = {**variant_hypothesis(index).model_dump(), "regime": regime, "confirmation": confirmation}
                    program, legacy = executable_program(h)
                    with self.subTest(family=h["family"], regime=regime, confirmation=confirmation):
                        self.assertTrue(legacy)
                        np.testing.assert_array_equal(signals(data, h), program_signals(data, program)[0])

    def test_export_source_manifest_sidecar_and_archive(self):
        data = demo_data().iloc[:80].copy()
        data["external_event"] = np.linspace(0, 1, len(data))
        h = hypothesis("col('external_event') > .3").model_dump()
        signature = hypothesis_signature(h)
        with tempfile.TemporaryDirectory() as tmp:
            artifact = export_mql5(h, "BTCUSD", data, Path(tmp)/"ea", signature, asdict(Settings()))
            source = Path(artifact["source"]).read_text(encoding="utf-8")
            self.assertNotIn("@@", source)
            self.assertIn("void OnTick()", source)
            self.assertIn("input bool EnableTrading=false", source)
            manifest = json.loads(Path(artifact["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["compilation"], "PENDING_METAEDITOR")
            self.assertEqual(manifest["mt5_parity"], "PENDING_STRATEGY_TESTER")
            self.assertEqual(manifest["source_sha256"], hashlib.sha256(Path(artifact["source"]).read_bytes()).hexdigest())
            csv = pd.read_csv(Path(artifact["directory"])/manifest["feature_file"], sep=";")
            np.testing.assert_allclose(csv.external_event, data.external_event)
            self.assertEqual(csv.timestamp_utc.iloc[0], int(data.timestamp.iloc[0].timestamp()))
            with zipfile.ZipFile(artifact["bundle"]) as bundle:
                self.assertIn("strategy.mq5", bundle.namelist())
                self.assertIn(manifest["feature_file"], bundle.namelist())
                self.assertIn("reference_signals.csv", bundle.namelist())
            with self.assertRaises(FileExistsError):
                export_mql5(h, "BTCUSD", data, Path(tmp)/"ea", signature, asdict(Settings()))

    def test_missing_feature_and_injection_fail_before_writing(self):
        data = demo_data()
        h = hypothesis("col('missing') > 0").model_dump()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)/"ea"
            with self.assertRaisesRegex(ValueError, "Faltan datos"):
                export_mql5(h, "BTCUSD", data, folder, hypothesis_signature(h), {})
            self.assertFalse(folder.exists())
            with self.assertRaises(ValueError):
                generate_source(h, "BTCUSD", data.timestamp.iloc[0], "features.csv", "a"*8+"\nINJECT")
            with self.assertRaises(ValueError):
                export_mql5(h, "../bad", data, folder, hypothesis_signature(h), {})

    def test_graph_contains_export_between_generation_and_reporting(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent(demo_data(), Settings(), Path(tmp)/"run", demo=True)
            edges = {(edge.source, edge.target) for edge in agent.build_graph().get_graph().edges}
            self.assertIn(("production_coder_node", "mql5_export_node"), edges)
            self.assertIn(("mql5_export_node", "reporter_node"), edges)
            self.assertNotIn(("production_coder_node", "reporter_node"), edges)
            self.assertEqual(agent.mql5_export_node(initial_state())["status"], "REJECTED")
            agent.demo = False
            with self.assertRaises(RuntimeError): agent.mql5_export_node(initial_state())
            self.assertFalse((Path(tmp)/"mql5").exists())

    @unittest.skipUnless(shutil.which("g++"), "C++ compiler unavailable; MetaEditor is a separate validation")
    def test_numerical_mql_kernel_matches_python_on_missing_and_nested_series(self):
        """Execute the actual MQL arithmetic bodies with a C++ array/API shim.

        This checks calculations, NOT MetaEditor compilation or broker execution.
        """
        data = demo_data().iloc[:90].copy()
        data["x"] = np.sin(np.arange(len(data)) / 3.)
        data["y"] = np.cos(np.arange(len(data)) / 4.)
        data.loc[[0, 4, 10, 22, 60], "x"] = np.nan
        expressions = [
            "sma(col('x'),5) > .2", "std(col('x'),5) > .4", "ema(col('x'),5) > .1",
            "lowest(col('x'),5) < -.3", "highest(col('x'),5) > .8",
            "total(col('x'),5) > 1", "rank(col('x'),5) > .55",
            "quantile(col('x'),5,.3) < -.2", "corr(col('x'),col('y'),5) > .2",
            "lag(col('x'),3) != 0", "change(col('x'),3) < -.1", "pct(col('x'),2) > .1",
            "log(abs(col('x'))) < -.6", "sqrt(abs(col('x'))) > .7",
            "where(col('x') > .5,col('x'),col('y')) > .3",
            "(col('x') > .2) and (col('y') < -.1)", "(col('x') > .2) or (col('y') < -.1)",
            "not (col('x') > .2)", "sma(lag(ema(col('x'),3),2),4) >= .3",
            "-col('x') / (col('y') + 1) <= .3", "col('x') == col('x')",
            "corr(col('x'),col('x'),1) > 0", "ema(col('x'),1) > .1",
        ]
        template = Path(__file__).with_name("mql5_runtime.mq5.tmpl").read_text(encoding="utf-8")
        common = template[template.index("bool Valid("):template.index("datetime UtcTime(")]
        kernel = template[template.index("double Binary("):template.index("bool EvaluateSignals(")]
        kernel = kernel.replace("double values[];", "std::vector<double> values;")
        prefix = r'''
#include <algorithm>
#include <cmath>
#include <cfloat>
#include <iomanip>
#include <iostream>
#include <map>
#include <string>
#include <vector>
using string=std::string;
const double EMPTY_VALUE=DBL_MAX;
bool MathIsValidNumber(double x){return std::isfinite(x);}
double MathMin(double a,double b){return std::min(a,b);}
double MathMax(double a,double b){return std::max(a,b);}
double MathSqrt(double x){return std::sqrt(x);}
double MathAbs(double x){return std::abs(x);}
double MathLog(double x){return std::log(x);}
double MathFloor(double x){return std::floor(x);}
double MathCeil(double x){return std::ceil(x);}
template<class T> void ArrayResize(std::vector<T>& a,int n){a.resize(n);}
void ArrayInitialize(std::vector<double>& a,double v){std::fill(a.begin(),a.end(),v);}
void ArraySort(std::vector<double>& a){std::sort(a.begin(),a.end());}
struct RuleNode {string op; int a,b,c; double number; string field; std::vector<double> v;};
std::vector<RuleNode> nodes;
std::map<string,std::vector<double>> fields;
double FieldValue(string name,int i){return fields.at(name).at(i);}
'''
        main = ["int main(){"]
        for field in ("x", "y"):
            values = ",".join(format(v, ".17g") if np.isfinite(v) else "EMPTY_VALUE" for v in data[field])
            main.append(f'fields["{field}"]={{{values}}};')
        expected = []
        for expression in expressions:
            program = {"entry": expression, "exit": "False", "parameters": []}
            nodes, roots = compile_program(program)
            main.append(f"nodes.clear(); nodes.resize({len(nodes)});")
            for i, node in enumerate(nodes):
                a, b, c = (node["args"] + [-1]*3)[:3]
                main.append(f"nodes[{i}]={{{mql_string(node['op'])},{a},{b},{c},{node['value']:.17g},{mql_string(node['field'])},{{}}}};")
            main.append(f"for(int j=0;j<{len(nodes)};j++) EvaluateNode(j,{len(data)});")
            main.append(f"for(int i=0;i<{len(data)};i++) std::cout << (Yes(nodes[{roots[0]}].v[i]) ? '1' : '0'); std::cout << '\\n';")
            expected.append(program_signals(data, program)[0])
        main.append("}")
        with tempfile.TemporaryDirectory() as tmp:
            cpp, exe = Path(tmp)/"kernel.cpp", Path(tmp)/"kernel.exe"
            compat = "const bool EmaSpan3GapCorrection=" + str(ema_span3_gap_correction()).lower() + ";\n"
            cpp.write_text(prefix + compat + common + kernel + "\n".join(main), encoding="utf-8")
            compilation = subprocess.run([shutil.which("g++"), "-std=c++17", str(cpp), "-o", str(exe)], capture_output=True, text=True, timeout=60)
            self.assertEqual(compilation.returncode, 0, compilation.stderr)
            result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20, check=True)
        rows = result.stdout.splitlines()
        self.assertEqual(len(rows), len(expected))
        for expression, actual, wanted in zip(expressions, rows, expected):
            with self.subTest(expression=expression):
                np.testing.assert_array_equal(np.array(list(actual)) == "1", wanted)


if __name__ == "__main__":
    unittest.main()
