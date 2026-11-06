#!/usr/bin/env python3
"""Review every past run and suggest per-Actor limits for config/actors.json.

    python3 scripts/review_run_limits.py                 # storage/runs
    python3 scripts/review_run_limits.py --runs DIR

This is how the defaults in config/actors.json were chosen (2026-10-23, 2,598
runs), kept as a tool so they can be re-derived as more runs accumulate instead
of being guessed again.

What it reads: each run's run.json -- actor, status, duration_seconds and, for
runs from after per-run limits existed, peak_memory_mb. Read as data only
(json.loads on a bounded, regular file); a run directory is written by the
Actor's container, so nothing in it is trusted further than that.

The suggestion rules:
  timeout default  = 2.5 x the longest SUCCESSFUL run (cancelled and failed runs
                     are shown, not used: a run someone stopped by hand says
                     nothing about how long the work takes), rounded up to a
                     multiple of 15 minutes, never under 10 minutes;
  timeout max      = 3 x the default;
  memory default   = 1.5 x the highest recorded peak, rounded up to 256 MB --
                     or "not measured yet" when no run recorded a peak.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_RECORD_BYTES = 1024 * 1024


def _read(path: Path) -> dict | None:
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(p * len(ordered)))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=Path, default=ROOT / "storage" / "runs")
    args = parser.parse_args()

    durations: dict[str, list[float]] = defaultdict(list)
    longest_any: dict[str, float] = defaultdict(float)
    peaks: dict[str, list[int]] = defaultdict(list)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    ooms: dict[str, int] = defaultdict(int)

    for entry in args.runs.iterdir():
        record = _read(entry / "run.json")
        if not record or not isinstance(record.get("actor"), str):
            continue
        actor, status = record["actor"], str(record.get("status"))
        counts[actor][status] += 1
        seconds = record.get("duration_seconds")
        if isinstance(seconds, (int, float)):
            longest_any[actor] = max(longest_any[actor], float(seconds))
            if status == "SUCCEEDED":
                durations[actor].append(float(seconds))
        peak = record.get("peak_memory_mb")
        if isinstance(peak, int) and peak > 0:
            peaks[actor].append(peak)
        if "out of memory" in str(record.get("error") or ""):
            ooms[actor] += 1

    if not counts:
        print(f"no runs under {args.runs}")
        return 1

    print(f"{'actor':16} {'ok':>5} {'p50':>6} {'p95':>6} {'longest':>8} {'any':>7} "
          f"{'peakMB':>7} {'oom':>4}  suggestion")
    for actor in sorted(counts):
        ok = durations.get(actor, [])
        longest = max(ok) if ok else 0.0
        timeout = max(10, math.ceil(longest * 2.5 / 60 / 15) * 15) if ok else None
        peak = max(peaks[actor]) if peaks.get(actor) else None
        memory = math.ceil(peak * 1.5 / 256) * 256 if peak else None
        suggestion = []
        if timeout:
            suggestion.append(f"timeout {timeout}/{timeout * 3} min")
        suggestion.append(f"memory {memory} MB" if memory else "memory: not measured yet")
        print(f"{actor:16} {len(ok):5d} "
              f"{(_pct(ok, .5) if ok else 0):6.0f} {(_pct(ok, .95) if ok else 0):6.0f} "
              f"{longest:8.0f} {longest_any[actor]:7.0f} "
              f"{(peak or 0):7d} {ooms[actor]:4d}  {', '.join(suggestion)}")
    print("\n(durations in seconds; 'any' = longest run of any status)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
