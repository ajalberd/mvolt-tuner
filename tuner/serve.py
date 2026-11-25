"""Local web server for the tuner UI.

Serves the static UI and a small API:

    GET  /api/picks       recommended configs, from the journal
    GET  /api/profiles    existing mVolt+ profiles (read-only)
    GET  /api/importance  knob effects
    POST /api/profile     {"name": "...", "params": {"core": 150}} -> writes a profile

Bound to 127.0.0.1 only, and that alone is not enough: any web page the user
visits can send requests to localhost. So:
  * only web/ and the two UI data files are served (never state/journal.db,
    config, or the rest of the project);
  * the Host header must be this server (blocks DNS rebinding);
  * POST needs Content-Type: application/json (forces a CORS preflight, which
    this server never approves) and a same-origin Origin / Sec-Fetch-Site;
  * every param is validated against the knob registry (known controls, finite
    numbers); there are no tuner-imposed offset ceilings.
Writing a profile requires mVolt+ to be closed, and the route says so.
"""
from __future__ import annotations

import json
import posixpath
import re
import secrets
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

from . import config, knobs, profiles, state

DEFAULT_PORT = 8712
MAX_BODY = 64 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,64}$")
STATIC_PREFIXES = ("/web/",)
STATIC_FILES = ("/data/live.json", "/data/recommendation.json")
POST_ROUTES = ("/api/profile", "/api/limits", "/api/plan/preview", "/api/run", "/api/bench",
               "/api/run/stop", "/api/probe", "/api/search/defaults", "/api/search/plan",
               "/api/search/edge-preview")
SESSION_TOKEN = secrets.token_urlsafe(24)
_lock = threading.Lock()


def _journal_trials() -> list[dict[str, Any]]:
    j = state.Journal()
    try:
        return j.scored_trials()
    finally:
        j.close()


def _picks_payload() -> dict[str, Any]:
    from . import setup as _setup
    picks = profiles.pick_configs(_journal_trials())
    return {
        "limits": _setup.load_limits(),
        "app_running": profiles.app_running(),
        "picks": [
            {"index": i + 1, "label": label, "params": params, "stats": stats,
             "savable": bool(params), "suggested_name": f"tuner-best-{i + 1}"}
            for i, (label, params, stats) in enumerate(picks)
        ],
    }


def validate_profile_request(data: Any) -> tuple[str, dict[str, Any]]:
    """Raise ValueError with a user-facing message if the request is unacceptable."""
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    name = str(data.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("name must be 1-64 letters, digits, spaces, '.', '_' or '-'")
    params = data.get("params")
    if not isinstance(params, dict) or not params:
        raise ValueError("params must be a non-empty object")
    problems = knobs.validate_params(params)
    if problems:
        raise ValueError("; ".join(problems))
    unsavable = sorted(k for k in params if k not in profiles.SAVABLE)
    if unsavable:
        raise ValueError(f"cannot be saved in a profile: {', '.join(unsavable)}")
    return name, params


class Handler(SimpleHTTPRequestHandler):
    server_version = "mvolt-tuner"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(config.ROOT), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
        pass

    # Pages change when the tuner is updated; without this a browser keeps
    # serving its cached copy (e.g. an old run.html) until a hard refresh.
    def send_header(self, keyword: str, value: str) -> None:
        if keyword.lower() == "cache-control":
            self._cache_set = True
        super().send_header(keyword, value)

    def end_headers(self) -> None:
        if not getattr(self, "_cache_set", False):
            super().send_header("Cache-Control", "no-cache")
        self._cache_set = False
        super().end_headers()

    # ---- guards ----
    def _allowed_hosts(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").lower() in self._allowed_hosts()

    def _same_origin(self) -> bool:
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site and site not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if origin is not None:
            o = urlsplit(origin)
            if o.scheme != "http" or (o.netloc or "").lower() not in self._allowed_hosts():
                return False
        return True

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def static_allowed(raw_path: str) -> bool:
        path = posixpath.normpath(unquote(raw_path.split("?")[0].split("#")[0]))
        if "\\" in path or "\x00" in path:
            return False
        return path in STATIC_FILES or any(path.startswith(p) for p in STATIC_PREFIXES)

    # ---- routes ----
    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json(421, {"error": "unexpected Host header"})
            return
        route = self.path.split("?")[0]
        try:
            if route == "/api/session":
                self._json(200, {"token": SESSION_TOKEN})
                return
            if route == "/api/state":
                from . import webapi
                self._json(200, webapi.gpu_state())
                return
            if route == "/api/run":
                from . import webapi
                self._json(200, webapi.run_status())
                return
            if route == "/api/search":
                from . import webapi
                self._json(200, webapi.search_status())
                return
            if route == "/api/bench":
                from . import webapi
                self._json(200, webapi.bench_info())
                return
            if route == "/api/crashes":
                from . import webapi
                self._json(200, webapi.crash_log())
                return
            if route == "/api/search/analysis":
                from . import webapi
                try:
                    self._json(200, webapi.analyse())
                except ValueError as exc:
                    self._json(409, {"error": str(exc)})
                return
            if route == "/api/picks":
                self._json(200, _picks_payload())
                return
            if route == "/api/profiles":
                self._json(200, {"app_running": profiles.app_running(),
                                 "profiles": profiles.read_profiles()})
                return
            if route == "/api/importance":
                from . import analysis, importance, optuna_report
                j = state.Journal()
                try:
                    swept = j.get_meta("swept_knobs", "") or ""
                finally:
                    j.close()
                only = {k for k in swept.split(",") if k} or None
                trials = _journal_trials()
                # eta^2 fields stay at the top level for older clients.
                payload = importance.knob_effects(trials, only=only)
                payload["analysis"] = analysis.summary(trials, only)
                payload["optuna"] = optuna_report.report(trials, only)
                self._json(200, payload)
                return
        except Exception as exc:
            self._json(500, {"error": str(exc)})
            return
        if route in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/web/run.html")
            self.end_headers()
            return
        if not self.static_allowed(self.path):
            self._json(404, {"error": "not found"})
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        if not self._host_ok() or not self.static_allowed(self.path):
            self.send_error(404)
            return
        super().do_HEAD()

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json(421, {"error": "unexpected Host header"})
            return
        route = self.path.split("?")[0]
        if route not in POST_ROUTES:
            self._json(404, {"error": "not found"})
            return
        if not self._same_origin():
            self._json(403, {"error": "cross-origin request refused"})
            return
        # Every write needs the token from GET /api/session, which only a page
        # served by this server can read (browsers block cross-origin reads).
        if not secrets.compare_digest(self.headers.get("X-Tuner-Token") or "", SESSION_TOKEN):
            self._json(403, {"error": "missing or wrong session token - reload the page"})
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._json(415, {"error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._json(413, {"error": "body too large"})
            return
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"error": "invalid JSON body"})
            return
        if route != "/api/profile":
            self._web_action(route, data)
            return
        try:
            name, params = validate_profile_request(data)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
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

    def _web_action(self, route: str, data: Any) -> None:
        from . import webapi
        if not isinstance(data, dict):
            self._json(400, {"error": "body must be a JSON object"})
            return
        try:
            if route == "/api/limits":
                self._json(200, webapi.save_limits(data))
            elif route == "/api/plan/preview":
                self._json(200, webapi.preview(data))
            elif route == "/api/search/defaults":
                self._json(200, webapi.search_defaults(str(data.get("profile") or "")))
            elif route == "/api/search/edge-preview":
                from . import edge
                plan = webapi.edge_plan_from(data)
                self._json(200, {"plan": plan, "estimate_runs": edge.estimate_runs(plan, webapi._trials()),
                                 "lanes": len(plan["budgets"]) * len(plan["rels"])})
            elif route == "/api/search/plan":
                plan = webapi.search_plan(data)
                plan.pop("points")
                self._json(200, plan)
            elif route == "/api/run":
                self._json(200, webapi.start_run(data))
            elif route == "/api/run/stop":
                self._json(200, webapi.stop_run())
            elif route == "/api/bench":
                self._json(200, webapi.switch_bench(str(data.get("tool") or "")))
            elif route == "/api/probe":
                self._json(200, webapi.probe())
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except PermissionError as exc:
            self._json(403, {"error": str(exc)})
        except RuntimeError as exc:
            self._json(409, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})


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
