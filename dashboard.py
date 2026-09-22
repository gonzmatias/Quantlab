"""Interfaz local de Quant Lab. Ejecutar: python dashboard.py [--port 8765]."""
from __future__ import annotations

import argparse
import copy
import io
import json
import logging
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

from trading_agent import Settings, TradingAgent, RunCancelled, demo_data, initial_state, validate_data, market_settings
from market_data import ASSETS, load_public_data, load_market_manifest

ROOT = Path(__file__).resolve().parent
MAX_BODY = 16 * 1024 * 1024


class JobManager:
    """Un solo trabajo a la vez, con cancelación cooperativa y snapshots sin secretos."""
    def __init__(self, output_root: Path | None = None):
        self.output_root = output_root or ROOT / "outputs"
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.busy = False
        self.state = initial_state()
        self.revision = 0
        self.events = []
        self.output = None
        self.settings = Settings()
        self.synthetic = True
        self.error = ""
        self.thread = None
        self.data_info = {}

    def update(self, state):
        with self.lock:
            before = (self.state.get("current_stage"), self.state.get("iteration_count"), self.state.get("lifecycle"))
            after = (state.get("current_stage"), state.get("iteration_count"), state.get("lifecycle"))
            self.state = copy.deepcopy(state)
            if before != after:
                self.events.append({"time": datetime.now(timezone.utc).isoformat(), "stage": after[0],
                                    "attempt": after[1], "lifecycle": after[2]})
                self.events = self.events[-200:]
            self.revision += 1

    def start(self, payload: dict):
        mode = payload.get("mode", "public")
        if mode not in ("demo", "public", "local"):
            raise ValueError("Selecciona demo o datos públicos")
        run_mode = payload.get("run_mode", "search")
        if run_mode not in ("search", "single"):
            raise ValueError("Selecciona investigación o test único")
        hypothesis = payload.get("hypothesis", "") if run_mode == "single" else None
        if run_mode == "single":
            if not isinstance(hypothesis, str) or not 20 <= len(hypothesis.strip()) <= 20000:
                raise ValueError("Describe la hipótesis con entre 20 y 20000 caracteres")
            if mode == "demo":
                raise ValueError("El test único requiere datos públicos o del intermediario y un modelo de IA")
        settings = dict(payload.get("settings", {}))
        if run_mode == "single":
            settings["max_iterations"] = 1
        cfg = Settings(**settings)
        model = str(payload.get("model", "")).strip() or os.getenv("OPENAI_MODEL")
        key = str(payload.get("api_key", "")).strip() or None
        if mode != "demo" and not ((key or os.getenv("OPENAI_API_KEY")) and model):
            raise ValueError("Indica el modelo de IA y la clave API en Configuración; los precios públicos no requieren clave")
        profiles = payload.get("asset_settings", {})
        if not isinstance(profiles, dict) or set(profiles) - set(ASSETS):
            raise ValueError("Configuración de activos inválida")
        for asset in ASSETS:
            market_settings(cfg, asset, profiles.get(asset))
        with self.lock:
            if self.busy:
                raise RuntimeError("Ya hay una búsqueda activa")
        data = {a: demo_data(cfg.seed+i) for i, a in enumerate(ASSETS)} if mode == "demo" else None
        local_metadata = None
        if mode == "local":
            manifest = payload.get("data_manifest", "")
            if not manifest:
                raise ValueError("Indica un manifiesto local de datos")
            data, local_metadata, profiles = load_market_manifest(manifest)
        with self.lock:
            if self.busy:
                raise RuntimeError("Ya hay una búsqueda activa")
            self.settings, self.synthetic, self.error = cfg, mode == "demo", ""
            self.state = {**initial_state(), "lifecycle": "STARTING"}
            self.data_info = {"name": "Demo · cinco series sintéticas" if self.synthetic else "Dukascopy + Coinbase · descargando", "assets": {}}
            self.output = self.output_root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            self.events = []
            self.stop.clear()
            self.busy = True
            self.revision += 1
            self.thread = threading.Thread(target=self._work, args=(data, cfg, self.output, model, self.synthetic, key, profiles, local_metadata, hypothesis), daemon=True)
            self.thread.start()

    def _work(self, data, cfg, output, model, synthetic, key, profiles=None, local_metadata=None, hypothesis=None):
        try:
            metadata = local_metadata or {}
            def checkpoint():
                if self.stop.is_set():
                    raise RunCancelled()
            def progress(message):
                with self.lock:
                    self.state["strategy_name"] = message
                    self.state["logs"] = (self.state["logs"] + [message])[-300:]
                    self.revision += 1
            if data is None:
                data, metadata = load_public_data(self.output_root / "market_cache", checkpoint, progress)
            checkpoint()
            with self.lock:
                self.data_info = {"name": "Demo sintética" if synthetic else "Contrato local" if local_metadata else "Dukascopy + Coinbase · diario", "assets": metadata}
            agent = TradingAgent(data, cfg, output, model, synthetic, on_event=self.update,
                                 stop_requested=self.stop.is_set, api_key=key, market_metadata=metadata, asset_settings=profiles,
                                 natural_hypothesis=hypothesis)
            agent.run()
        except RunCancelled:
            with self.lock:
                self.state.update(lifecycle="CANCELLED", status="REJECTED")
        except Exception as exc:
            logging.exception("Búsqueda interrumpida por error operativo")
            with self.lock:
                self.error = str(exc).replace(key, "[clave omitida]") if key else str(exc)
                self.state.update(lifecycle="ERROR", status="REJECTED")
        finally:
            with self.lock:
                self.busy = False
                self.revision += 1

    def cancel(self):
        with self.lock:
            if self.busy:
                self.stop.set()
                self.revision += 1

    def snapshot(self):
        with self.lock:
            state = copy.deepcopy(self.state)
            for field in ("prototype_code", "production_code", "history", "latest_report"):
                state.pop(field, None)
            state["reports"] = [{"attempt": r["attempt"], "strategy_name": r["strategy_name"],
                                 "outcome": r["outcome"], "reasons": r["rejection_reasons"],
                                 "research_outcome": r.get("research_outcome"),
                                 "selected_asset": r.get("selected_asset", ""),
                                 "oos": r["quant_metrics"].get("out_of_sample", {}),
                                 "dsr": r["quant_metrics"].get("dsr")} for r in state["reports"]]
            return {"revision": self.revision, "busy": self.busy, "stop_requested": self.stop.is_set(),
                    "state": state, "events": list(self.events), "synthetic": self.synthetic,
                    "max_iterations": self.settings.max_iterations, "data": self.data_info,
                    "error": self.error, "output": str(self.output) if self.output else None}


def create_server(port: int, manager: JobManager | None = None):
    jobs = manager or JobManager()
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, body, content_type="application/json; charset=utf-8", download=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{download}"')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def valid_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

        def do_GET(self):
            if not self.valid_host():
                return self.send(403, {"error": "Host no permitido"})
            url = urlparse(self.path)
            if url.path == "/":
                page = (ROOT / "web" / "index.html").read_text("utf-8").replace("__SESSION_TOKEN__", token)
                return self.send(200, page, "text/html; charset=utf-8")
            if url.path in ("/app.js", "/style.css"):
                content_type = "text/javascript; charset=utf-8" if url.path.endswith("js") else "text/css; charset=utf-8"
                return self.send(200, (ROOT / "web" / url.path[1:]).read_bytes(), content_type)
            if url.path == "/api/status":
                snapshot = jobs.snapshot()
                since = parse_qs(url.query).get("since", [""])[0]
                if since == str(snapshot["revision"]):
                    return self.send(200, {"unchanged": True, "revision": snapshot["revision"]})
                return self.send(200, snapshot)
            if url.path == "/api/config":
                return self.send(200, {"has_api_key": bool(os.getenv("OPENAI_API_KEY")), "model": os.getenv("OPENAI_MODEL", "")})
            match = re.fullmatch(r"/api/report/(\d{1,18})\.(json|html|md)", url.path)
            if match:
                with jobs.lock:
                    output = jobs.output
                if output:
                    attempt, extension = match.groups()
                    path = output / "reports" / f"attempt_{int(attempt):02}.{extension}"
                    if path.is_file():
                        types = {"json": "application/json; charset=utf-8", "html": "text/html; charset=utf-8", "md": "text/markdown; charset=utf-8"}
                        return self.send(200, path.read_bytes(), types[extension], path.name if "download" in parse_qs(url.query) else None)
            if url.path in ("/api/mql5/source", "/api/mql5/bundle"):
                with jobs.lock:
                    artifact = jobs.state.get("mql5_export", {})
                    key = "source" if url.path.endswith("/source") else "bundle"
                    if jobs.state["status"] == "APPROVED" and artifact.get("status") == "EXPORTED" and artifact.get(key):
                        path = Path(artifact[key])
                        if path.is_file():
                            content_type = "text/plain; charset=utf-8" if key == "source" else "application/zip"
                            return self.send(200, path.read_bytes(), content_type, path.name)
            if url.path == "/api/production":
                with jobs.lock:
                    if jobs.state["status"] == "APPROVED" and jobs.output:
                        path = jobs.output / "production_strategy.py"
                        if path.is_file():
                            return self.send(200, path.read_bytes(), "text/plain; charset=utf-8", path.name)
            self.send(404, {"error": "Recurso no encontrado"})

        def do_POST(self):
            if not self.valid_host() or self.headers.get("X-Session-Token") != token:
                return self.send(403, {"error": "Sesión inválida; recarga la interfaz"})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send(415, {"error": "Se requiere JSON"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    return self.send(413, {"error": "La solicitud supera el máximo de 16 MB"})
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Solicitud inválida")
                if self.path == "/api/start":
                    jobs.start(payload)
                    return self.send(202, {"started": True})
                if self.path == "/api/stop":
                    jobs.cancel()
                    return self.send(202, {"stop_requested": True})
                return self.send(404, {"error": "Acción desconocida"})
            except RuntimeError as exc:
                return self.send(409, {"error": str(exc)})
            except (ValueError, TypeError, KeyError) as exc:
                return self.send(400, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = create_server(args.port)
    print(f"Quant Lab listo: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
