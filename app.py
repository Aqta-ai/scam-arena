"""Scam Arena web app. Run: NEBIUS_API_KEY=... python app.py  then open http://127.0.0.1:8090"""
import glob
import json
import math
import os
import threading
import time
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import arena
from scenarios import BY_ID, SCENARIOS

HERE = Path(__file__).resolve().parent
MAX_BODY = 64_000


class RequestError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class AnalysisLimits:
    """Bound analysis starts per raw client address and model work in flight."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.starts = {}
        self.active = 0

    def acquire(self, client_ip):
        with self.lock:
            now = self.clock()
            for ip in list(self.starts):
                while self.starts[ip] and self.starts[ip][0] <= now - 60:
                    self.starts[ip].popleft()
                if not self.starts[ip]:
                    del self.starts[ip]
            recent = self.starts.get(client_ip)
            if recent and len(recent) >= 5:
                return max(1, math.ceil(60 - (now - recent[0])))
            if self.active >= 2:
                return 1
            self.starts.setdefault(client_ip, deque()).append(now)
            self.active += 1
            return None

    def release(self):
        with self.lock:
            self.active -= 1


ANALYSIS_LIMITS = AnalysisLimits()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(HERE / "static"), **k)

    def log_message(self, *a):
        pass

    def send_json(self, obj, status=200, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, str(value))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise RequestError("Invalid request length.") from exc
        if n < 0:
            raise RequestError("Invalid request length.")
        if n > MAX_BODY:
            raise RequestError("The request is too large.", 413)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, UnicodeError) as exc:
            raise RequestError("Send a valid JSON object.") from exc
        if not isinstance(body, dict):
            raise RequestError("Send a JSON object.")
        return body

    def analyse_message(self, body):
        try:
            text = arena.validate_analysis_text(body.get("text"))
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, 400)
        if not os.environ.get("NEBIUS_API_KEY"):
            return self.send_json({"error": "Live analysis is unavailable. The server needs a Nebius API key. Recorded examples are still available."}, 503)
        retry_after = ANALYSIS_LIMITS.acquire(self.client_address[0])
        if retry_after is not None:
            return self.send_json({"error": "Analysis is busy or you have reached five messages per minute. Please try again shortly."},
                                  429, {"Retry-After": retry_after})
        try:
            return self.send_json(arena.analyse(text))
        except arena.AnalysisResponseError:
            return self.send_json({"error": "Nemotron could not produce an evidence-grounded review. Please try again."}, 502)
        except Exception:
            return self.send_json({"error": "The message analysis could not be completed. Please try again."}, 502)
        finally:
            ANALYSIS_LIMITS.release()

    def do_GET(self):
        if self.path == "/api/scenarios":
            return self.send_json({"model": arena.MODEL, "conditions": list(arena.CONDITIONS),
                "scenarios": [{k: s[k] for k in ("id", "title", "hook", "sender")} for s in SCENARIOS]})
        if self.path == "/api/results/bouts":
            files = sorted(glob.glob(str(HERE / "results" / "eval-*.json")))
            if not files:
                return self.send_json({"bouts": []})
            d = json.loads(Path(files[-1]).read_text())
            return self.send_json({"bouts": [b for b in d["bouts"] if b.get("repeat") == 0], "live": bool(os.environ.get("NEBIUS_API_KEY"))})
        if self.path == "/api/results/latest":
            files = sorted(glob.glob(str(HERE / "results" / "eval-*.json")))
            if not files:
                return self.send_json({"summary": None})
            d = json.loads(Path(files[-1]).read_text())
            return self.send_json({k: d[k] for k in ("started_utc", "model", "provider", "repeats", "summary")})
        return super().do_GET()

    def do_POST(self):
        try:
            body = self.read_json()
            if self.path == "/api/analyse":
                return self.analyse_message(body)
            sid = body.get("scenario")
            if sid not in BY_ID:
                return self.send_json({"error": "unknown scenario"}, 400)
            if self.path != "/api/rehearse/next" and not os.environ.get("NEBIUS_API_KEY"):
                return self.send_json({"error": "Replay mode: set NEBIUS_API_KEY to run Nemotron live."}, 503)
            if self.path == "/api/rehearse/next":
                history = [h for h in body.get("history", []) if h.get("role") in ("scammer", "person")][:12]
                return self.send_json({"message": arena.scammer_turn(sid, history)})
            if self.path == "/api/rehearse/debrief":
                history = [h for h in body.get("history", []) if h.get("role") in ("scammer", "person")][:12]
                return self.send_json(arena.debrief(sid, history))
            if self.path == "/api/eval/bout":
                cond = body.get("condition")
                if cond not in arena.CONDITIONS:
                    return self.send_json({"error": "unknown condition"}, 400)
                return self.send_json(arena.run_bout(sid, cond))
            return self.send_json({"error": "not found"}, 404)
        except RequestError as exc:
            return self.send_json({"error": str(exc)}, exc.status)
        except Exception as exc:  # keep stack traces out of the browser
            return self.send_json({"error": "The model request failed. Try again.", "detail": type(exc).__name__}, 502)


if __name__ == "__main__":
    if not os.environ.get("NEBIUS_API_KEY"):
        print("No NEBIUS_API_KEY: replay mode. Recorded Nemotron conversations are shown; live runs and the coach are off.")
    port = int(os.environ.get("PORT", "8090"))
    print(f"Scam Arena on http://127.0.0.1:{port}  model {arena.MODEL}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
