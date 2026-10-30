"""Run the agent evaluation: every scenario x condition x repeat, same scripted messages each time.

    NEBIUS_API_KEY=... python run_eval.py --repeats 3
"""
import argparse
import concurrent.futures as cf
import json
import time
from pathlib import Path

import arena
from scenarios import SCENARIOS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    jobs = [(s["id"], c, r) for s in SCENARIOS for c in arena.CONDITIONS for r in range(a.repeats)]
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    bouts = []
    with cf.ThreadPoolExecutor(a.workers) as pool:
        futs = {pool.submit(arena.run_bout, sid, cond): (sid, cond, r) for sid, cond, r in jobs}
        for f in cf.as_completed(futs):
            sid, cond, r = futs[f]
            try:
                b = f.result()
            except Exception as exc:  # report failures, never drop them silently
                b = {"scenario": sid, "condition": cond, "outcome": "error", "error": str(exc)}
            b["repeat"] = r
            bouts.append(b)
            print(f"{cond:15s} {sid:18s} {b['outcome']}")
    summary = {}
    for cond in arena.CONDITIONS:
        rows = [b for b in bouts if b["condition"] == cond]
        summary[cond] = {k: sum(1 for b in rows if b["outcome"] == k) for k in ("compromised", "verified", "resisted", "error")}
        summary[cond]["total"] = len(rows)
    out = {"started_utc": started, "model": arena.MODEL, "provider": "Nebius Token Factory",
           "repeats": a.repeats, "summary": summary, "usage": arena.USAGE, "bouts": bouts}
    path = Path("results") / f"eval-{started.replace(':', '')}.json"
    path.write_text(json.dumps(out, indent=2))
    print(json.dumps(summary, indent=2), arena.USAGE, "->", path)


if __name__ == "__main__":
    main()
