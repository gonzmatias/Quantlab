"""Local server and synthetic artifact for the browser acceptance test."""
import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard import JobManager, create_server
from gherkin_pipeline import save_json, translate
from test_tick_pipeline import TEXT, TickPipelineTests, translator


fixture = TickPipelineTests()
fixture.setUp()
try:
    bundle = translate(TEXT, translator=translator)
    manifest = fixture.manifest(bundle["document"])
    artifact = fixture.root / "artifact.json"
    save_json(artifact, bundle)
    server = create_server(0, JobManager(fixture.root / "jobs"))
    print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}",
                      "artifact": str(artifact), "manifest": str(manifest)}), flush=True)
    with patch("gherkin_pipeline.ask_model", side_effect=AssertionError("AI forbidden")), \
         patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")), \
         patch.dict("os.environ", {}, clear=True):
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        sys.stdin.read()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
finally:
    fixture.doCleanups()
