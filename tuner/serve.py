"""Local web server for the tuner UI.

Serves the static UI *and* the two API routes the pick buttons need:

    GET  /api/picks      recommended configs, from the journal
    POST /api/profile    {"name": "...", "params": {"core": 150}} -> writes a profile

Bound to 127.0.0.1 only. Writing a profile requires mVolt+ to be closed, and the
route says so explicitly rather than corrupting the document.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import config, profiles, state

DEFAULT_PORT = 8712
_lock = threading.Lock()


def _journal_trials() -> list[dict[str, Any]]:
    out = []
    for t in state.Journal().trials():
        try:
            params = json.loads(t["params_json"])
        except Exception:
            params = {}
        out.append({"params": params, "score": t["score"], "watts": t["watts_avg"]})
    return out


def _picks_payload() -> dict[str, Any]:
    picks = profiles.pick_configs(_journal_trials(), config.MAX_AUTO_CORE_MHZ)
    return {
        "ceiling_mhz": config.MAX_AUTO_CORE_MHZ,
        "app_running": profiles.app_running(),
        "picks": [
            {"index": i + 1, "label": label, "params": params, "stats": stats,
             "suggested_name": f"tuner-best-{i + 1}"}
            for i, (label, params, stats) in enumerate(picks)
        ],
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(config.ROOT), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
        pass

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        route = self.path.split("?")[0]
        if route == "/api/picks":
            try:
                self._json(200, _picks_payload())
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return
        if route == "/api/profiles":
            try:
                self._json(200, {"app_running": profiles.app_running(),
                                 "profiles": profiles.read_profiles()})
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return
        if route == "/api/importance":
            try:
                from . import importance
                j = state.Journal()
                swept = j.get_meta("swept_knobs", "") or ""
                only = {k for k in swept.split(",") if k} or None
                self._json(200, importance.knob_effects(_journal_trials(), only=only))
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return
        if route in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/web/index.html")
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/api/profile":
            self._json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": "invalid JSON body"})
            return

        name = str(data.get("name") or "").strip()
        params = data.get("params") or {}
        if not name:
            self._json(400, {"error": "name is required"})
            return
        core = int(params.get("core") or 0)
        if core > config.MAX_AUTO_CORE_MHZ:
            self._json(400, {"error": f"core {core} exceeds the safe ceiling "
                                       f"{config.MAX_AUTO_CORE_MHZ} MHz"})
            return

        with _lock:
            try:
                info = profiles.write_profile(name, params)
            except profiles.ProfileError as exc:
                self._json(409, {"error": str(exc), "app_running": profiles.app_running()})
                return
            except Exception as exc:
                self._json(500, {"error": str(exc)})
                return
        self._json(200, {"ok": True, **info, "params": params})


def serve(port: int = DEFAULT_PORT) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mvolt-tuner UI  ->  http://127.0.0.1:{port}/")
    print(f"API              ->  http://127.0.0.1:{port}/api/picks")
    print("Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
