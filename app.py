"""Scam Arena web app. Run: NEBIUS_API_KEY=... python app.py  then open http://127.0.0.1:8090"""
import glob
import hashlib
import json
import math
import os
import threading
import time
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import arena
import custom
import vision
from scenarios import BY_ID, SCENARIOS

HERE = Path(__file__).resolve().parent
MAX_BODY = 4_000_000
RECORDED_MODELS = {
    "lightning": ("nvidia/Nemotron-3_5-Lightning", "results/knows-2026-10-30T034500Z.json",
                  "results/eval-2026-10-30T005400Z.json"),
    "super": ("nvidia/nemotron-3-super-120b-a12b",
              "results/models/nemotron-3-super-120b-a12b/knows-2026-10-30T092525Z.json",
              "results/models/nemotron-3-super-120b-a12b/eval-2026-10-30T092531Z.json"),
    "ultra": ("nvidia/Nemotron-3-Ultra-550b-a55b",
              "results/models/Nemotron-3-Ultra-550b-a55b/knows-2026-10-30T092525Z.json",
              "results/models/Nemotron-3-Ultra-550b-a55b/eval-2026-10-30T092531Z.json"),
}


class RequestError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class LiveLimits:
    """Shared live HTTP-start limits, not a count of individual model calls."""
    def __init__(self, clock=time.monotonic, wall_clock=time.time, daily_cap=None):
        self.clock = clock
        self.wall_clock = wall_clock
        self.daily_cap = max(0, int(os.environ.get("SCAM_ARENA_DAILY_CAP", "300"))) if daily_cap is None else daily_cap
        self.lock = threading.Lock()
        self.starts = {}
        self.active = 0
        self.day = None
        self.count = 0

    def acquire(self, client_ip):
        with self.lock:
            now = self.clock()
            utc_now = self.wall_clock()
            day = int(utc_now // 86400)
            if day != self.day:
                self.day, self.count = day, 0
            for ip in list(self.starts):
                while self.starts[ip] and self.starts[ip][0] <= now - 600:
                    self.starts[ip].popleft()
                if not self.starts[ip]:
                    del self.starts[ip]
            recent = self.starts.get(client_ip)
            if self.count >= self.daily_cap:
                return ("Today's live-run allowance has been reached. Recorded examples are still available.",
                        max(1, math.ceil((day + 1) * 86400 - utc_now)))
            if recent and len(recent) >= 4:
                return ("That is four live requests in ten minutes. Recorded examples are still available.",
                        max(1, math.ceil(600 - (now - recent[0]))))
            if self.active >= 2:
                return ("Two live requests are already running. Please try again shortly.", 1)
            self.starts.setdefault(client_ip, deque()).append(now)
            self.active += 1
            self.count += 1
            return None

    def release(self):
        with self.lock:
            self.active -= 1


LIVE_LIMITS = LiveLimits()


def server_address():
    """Keep local use on loopback unless the operator explicitly changes it."""
    return os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8090"))


def recorded_model(slug):
    """Read only the three pinned evidence pairs, checking their recorded hashes."""
    model, recognition_path, evaluation_path = RECORDED_MODELS[slug]
    inventory = json.loads((HERE / "results" / "evidence-summary.json").read_text())
    entry = next(item for item in inventory["models"] if item["id"] == model)
    hashes = {source["path"]: source["sha256"] for source in entry["sources"]}
    if set(hashes) != {recognition_path, evaluation_path}:
        raise ValueError("The recorded source inventory does not match.")
    documents = []
    for path in (recognition_path, evaluation_path):
        raw = (HERE / path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != hashes[path]:
            raise ValueError("The recorded source hash does not match.")
        documents.append(json.loads(raw))
    return {"model": model, "label": entry["label"], "recognition": documents[0], "evaluation": documents[1],
            "method": inventory["method"], "source_hashes": entry["sources"]}


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

    def run_live(self, work):
        if not os.environ.get("NEBIUS_API_KEY"):
            return self.send_json({"error": "Live runs are unavailable. The server needs a Nebius API key. Recorded examples are still available."}, 503)
        problem = LIVE_LIMITS.acquire(self.client_address[0])
        if problem is not None:
            return self.send_json({"error": problem[0]}, 429, {"Retry-After": problem[1]})
        try:
            return self.send_json(work())
        except arena.AnalysisResponseError:
            return self.send_json({"error": "Nemotron could not produce an evidence-grounded review. Please try again."}, 502)
        except vision.VisionResponseError:
            return self.send_json({"error": "The screenshot could not be transcribed completely. Try a clearer, tighter crop."}, 502)
        except Exception:
            return self.send_json({"error": "The model request could not be completed. Please try again."}, 502)
        finally:
            LIVE_LIMITS.release()

    def history(self, body):
        history = body.get("history", [])
        if not isinstance(history, list):
            raise RequestError("Send the conversation as a list.")
        checked = []
        for item in history[:12]:
            if not isinstance(item, dict):
                raise RequestError("Send valid conversation messages.")
            if item.get("role") not in ("scammer", "person"):
                continue
            content = item.get("content")
            if not isinstance(content, str) or len(content) > 4000:
                raise RequestError("Keep each conversation message to 4,000 characters or fewer.")
            checked.append({"role": item["role"], "content": content})
        return checked

    def do_GET(self):
        if self.path == "/api/results/models" or self.path.startswith("/api/results/models/"):
            slug = self.path.removeprefix("/api/results/models/")
            if slug not in RECORDED_MODELS:
                return self.send_json({"error": "Unknown recorded model."}, 404)
            try:
                result = recorded_model(slug)
            except (OSError, ValueError, KeyError, TypeError, StopIteration):
                return self.send_json({"error": "The recorded evidence is unavailable or does not match its inventory."}, 503)
            return self.send_json(result)
        if self.path == "/api/scenarios":
            return self.send_json({"model": arena.MODEL, "conditions": list(arena.CONDITIONS),
                "live": bool(os.environ.get("NEBIUS_API_KEY")),
                "image": {"model": vision.VL_MODEL, "max_bytes": vision.MAX_BYTES,
                          "max_side": vision.MAX_SIDE, "mime_types": list(vision.KINDS)},
                "scenarios": [{k: s[k] for k in ("id", "title", "hook", "sender")} for s in SCENARIOS]})
        if self.path == "/api/results/bouts":
            files = sorted(glob.glob(str(HERE / "results" / "eval-*.json")))
            if not files:
                return self.send_json({"bouts": []})
            d = json.loads(Path(files[-1]).read_text())
            return self.send_json({"bouts": [b for b in d["bouts"] if b.get("repeat") == 0], "live": bool(os.environ.get("NEBIUS_API_KEY"))})
        if self.path == "/api/results/knows":
            files = sorted(glob.glob(str(HERE / "results" / "knows-*.json")))
            return self.send_json(json.loads(Path(files[-1]).read_text()) if files else {"results": []})
        if self.path == "/api/results/evidence":
            evidence = HERE / "results" / "evidence-summary.json"
            if not evidence.exists():
                return self.send_json({"error": "Recorded evidence is not available in this copy."}, 404)
            return self.send_json(json.loads(evidence.read_text()))
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
            if self.path in ("/api/analyse", "/api/custom"):
                if self.path == "/api/custom" and set(body) != {"text"}:
                    raise RequestError("Send a JSON object with a text field only.")
                try:
                    text = arena.validate_analysis_text(body.get("text"))
                except ValueError as exc:
                    raise RequestError(str(exc)) from exc
                return self.run_live(lambda: arena.analyse(text) if self.path == "/api/analyse" else custom.both(text))
            if self.path == "/api/read-image":
                if set(body) != {"image"}:
                    raise RequestError("Send a JSON object with an image field only.")
                try:
                    image = vision.check(body.get("image"))
                except ValueError as exc:
                    raise RequestError(str(exc)) from exc
                return self.run_live(lambda: vision.read(image))
            if self.path not in ("/api/rehearse/next", "/api/rehearse/debrief", "/api/eval/bout", "/api/knows/ask"):
                return self.send_json({"error": "Not found."}, 404)
            sid = body.get("scenario")
            if not isinstance(sid, str) or sid not in BY_ID:
                return self.send_json({"error": "unknown scenario"}, 400)
            if self.path == "/api/rehearse/next":
                return self.send_json({"message": arena.scammer_turn(sid, self.history(body))})
            if self.path == "/api/rehearse/debrief":
                history = self.history(body)
                return self.run_live(lambda: arena.debrief(sid, history))
            if self.path == "/api/knows/ask":
                import run_knows
                return self.run_live(lambda: run_knows.ask(BY_ID[sid]))
            if self.path == "/api/eval/bout":
                cond = body.get("condition")
                if not isinstance(cond, str) or cond not in arena.CONDITIONS:
                    return self.send_json({"error": "unknown condition"}, 400)
                return self.run_live(lambda: arena.run_bout(sid, cond))
        except RequestError as exc:
            return self.send_json({"error": str(exc)}, exc.status)
        except Exception:  # keep stack traces and submitted content out of the browser
            return self.send_json({"error": "The request could not be completed. Please try again."}, 502)


if __name__ == "__main__":
    if not os.environ.get("NEBIUS_API_KEY"):
        print("No NEBIUS_API_KEY: replay mode. Recorded Nemotron conversations are shown; live runs and the coach are off.")
    host, port = server_address()
    print(f"Scam Arena on http://{host}:{port}  model {arena.MODEL}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
