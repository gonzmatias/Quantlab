import json
import re
import tempfile
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from dashboard import JobManager, create_server
from report_agent import build_report, render_html
from trading_agent import CodeNotes, Settings, TradingAgent, demo_data, initial_state
from test_trading_agent import H


class ReportingTests(unittest.TestCase):
    def test_rejections_get_reports_and_iterate(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            agent = TradingAgent(demo_data(), Settings(max_iterations=3), Path(tmp)/"run", demo=True,
                                 on_event=lambda state: events.append((state["current_stage"],state["iteration_count"])))
            result = agent.run()
            self.assertEqual(result["lifecycle"], "EXHAUSTED")
            self.assertEqual(len(result["reports"]), 3)
            for i in range(1,4):
                for suffix in ("json", "html", "md"):
                    self.assertTrue((agent.output/f"reports/attempt_{i:02}.{suffix}").exists())
            self.assertIn(("reporter_node",1), events)
            self.assertLess(events.index(("reporter_node",1)), events.index(("researcher_node",2)))
            first = json.loads((agent.output/"reports/attempt_01.json").read_text("utf-8"))
            self.assertIn("equity", first["charts"]["oos"])

    def test_report_escapes_model_text_and_marks_missing_tests(self):
        state = initial_state()
        state.update(status="REJECTED",hypothesis={**H,"rationale":"<script>alert(1)</script>"})
        report = build_report(state,asdict(Settings()),True)
        output = render_html(report)
        self.assertNotIn("<script>",output)
        self.assertIn("&lt;script&gt;",output)
        self.assertTrue(all(c["passed"] is None for c in report["checks"]))

    def test_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent(demo_data(),Settings(),Path(tmp)/"run",demo=True,stop_requested=lambda:True)
            state = agent.run()
            self.assertEqual(state["lifecycle"],"CANCELLED")
            self.assertEqual(state["reports"],[])

    def test_success_is_reported_and_ends_without_another_attempt(self):
        # Controlled node fixtures test orchestration, not evidence of financial alpha.
        class FakeLLM:
            def with_structured_output(self,*args,**kwargs): return self
            async def ainvoke(self,*args,**kwargs): return CodeNotes(summary="Test fixture",operational_notes=[])
        with tempfile.TemporaryDirectory() as tmp:
            agent = TradingAgent(demo_data(),Settings(),Path(tmp)/"run",demo=True)
            agent.demo = False
            agent.llm = FakeLLM()
            research = {"hypothesis":H,"iteration_count":1,"strategy_name":"Test strategy","status":"RESEARCHING"}
            with patch.object(agent,"final_validator_node",return_value={"final_validation":{"passed":True,"status":"PASSED","reason":"Fixture"}}), \
                 patch.object(agent,"researcher_node",return_value=research), \
                 patch.object(agent,"quant_validator_node",return_value={"quant_metrics":{"passed":True,"benchmark":{"passed":True},"advanced_tests":{key:{"passed":True} for key in ("regression","walk_forward")}}}), \
                 patch.object(agent,"stress_test_node",return_value={"stress_metrics":{"passed":True,"robustness_tests":{key:{"passed":True} for key in ("parameters","monte_carlo","signal_delay","concentration")}}}):
                result = agent.run()
            self.assertEqual(result["status"],"APPROVED")
            self.assertEqual(result["lifecycle"],"COMPLETED")
            self.assertEqual(result["iteration_count"],1)
            self.assertEqual(result["latest_report"]["outcome"],"APPROVED")
            self.assertTrue((agent.output/"production_strategy.py").exists())
            artifact = result["mql5_export"]
            self.assertEqual(artifact["status"], "EXPORTED")
            self.assertTrue(Path(artifact["source"]).is_file())
            self.assertTrue(Path(artifact["bundle"]).is_file())
            self.assertEqual(result["latest_report"]["mql5_export"], artifact)
            self.assertIn("mql5", Path(artifact["source"]).parts)


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = JobManager(Path(self.tmp.name)/"outputs")
        self.server = create_server(0,self.manager)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        with urlopen(self.base) as response:
            page = response.read().decode()
        self.token = re.search(r'name="session-token" content="([^"]+)"',page).group(1)

    def tearDown(self):
        self.manager.cancel()
        if self.manager.thread:
            self.manager.thread.join(timeout=10)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def post(self,path,payload,token=None):
        request = Request(self.base+path,data=json.dumps(payload).encode(),method="POST",
                          headers={"Content-Type":"application/json","X-Session-Token":self.token if token is None else token})
        return urlopen(request,timeout=20)

    def test_demo_reports_download_and_no_double_start(self):
        with self.post("/api/start",{"mode":"demo","settings":{"max_iterations":2}}) as response:
            self.assertEqual(response.status,202)
        self.manager.thread.join(timeout=20)
        with urlopen(self.base+"/api/status") as response:
            state=json.load(response)
        self.assertFalse(state["busy"])
        self.assertEqual(len(state["state"]["reports"]),2)
        self.assertEqual(state["state"]["lifecycle"],"EXHAUSTED")
        with urlopen(self.base+"/api/report/1.html?download=1") as response:
            self.assertIn("attachment",response.headers["Content-Disposition"])
            self.assertIn("RECHAZADO",response.read().decode())
        with urlopen(self.base+f"/api/status?since={state['revision']}") as response:
            self.assertTrue(json.load(response)["unchanged"])
        with self.manager.lock: self.manager.busy=True
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/start",{"mode":"demo"})
        self.assertEqual(caught.exception.code,409)
        with self.manager.lock: self.manager.busy=False

    def test_bad_input_and_session(self):
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/start",{"mode":"demo"},token="wrong")
        self.assertEqual(caught.exception.code,403)
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/start",{"mode":"demo","settings":{"max_iterations":-1}})
        self.assertEqual(caught.exception.code,400)
        self.assertFalse(self.manager.busy)
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/start",{"mode":"csv","csv":"not a CSV"})
        self.assertEqual(caught.exception.code,400)
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.base+"/api/report/../state.json")
        self.assertEqual(caught.exception.code,404)

    def test_mql_downloads_require_approved_export(self):
        folder = Path(self.tmp.name)/"ea"
        folder.mkdir()
        source, bundle = folder/"strategy.mq5", folder/"strategy_bundle.zip"
        source.write_text("// test fixture", encoding="utf-8")
        bundle.write_bytes(b"test bundle")
        with self.manager.lock:
            self.manager.state["mql5_export"] = {"status": "EXPORTED", "source": str(source), "bundle": str(bundle)}
        for endpoint in ("source", "bundle"):
            with self.assertRaises(HTTPError) as caught:
                urlopen(self.base+"/api/mql5/"+endpoint)
            self.assertEqual(caught.exception.code,404)
        with self.manager.lock:
            self.manager.state["status"] = "APPROVED"
        with urlopen(self.base+"/api/mql5/source") as response:
            self.assertIn("strategy.mq5", response.headers["Content-Disposition"])
            self.assertEqual(response.read(), source.read_bytes())
        with urlopen(self.base+"/api/mql5/bundle") as response:
            self.assertEqual(response.headers["Content-Type"], "application/zip")
            self.assertEqual(response.read(), bundle.read_bytes())

    def test_stop_endpoint(self):
        self.manager.busy=True
        with self.post("/api/stop",{}) as response:
            self.assertTrue(json.load(response)["stop_requested"])
        self.assertTrue(self.manager.stop.is_set())
        self.manager.busy=False


if __name__ == "__main__":
    unittest.main()
