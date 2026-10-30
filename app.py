"""Scam Arena web app. Run: NEBIUS_API_KEY=... python app.py  then open http://127.0.0.1:8090"""
import glob
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import arena
from scenarios import BY_ID, SCENARIOS

HERE = Path(__file__).resolve().parent
MAX_BODY = 64_000


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(HERE / "static"), **k)

    def log_message(self, *a):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path == "/api/scenarios":
            return self.send_json({"model": arena.MODEL, "conditions": list(arena.CONDITIONS),
                "scenarios": [{k: s[k] for k in ("id", "title", "hook")} for s in SCENARIOS]})
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
            sid = body.get("scenario")
            if sid not in BY_ID:
                return self.send_json({"error": "unknown scenario"}, 400)
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
        except Exception as exc:  # keep stack traces out of the browser
            return self.send_json({"error": "The model request failed. Try again.", "detail": type(exc).__name__}, 502)


if __name__ == "__main__":
    if not os.environ.get("NEBIUS_API_KEY"):
        raise SystemExit("Set NEBIUS_API_KEY (your Nebius Token Factory key) first.")
    port = int(os.environ.get("PORT", "8090"))
    print(f"Scam Arena on http://127.0.0.1:{port}  model {arena.MODEL}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
