"""Refuse a narration plan whose lines overrun their screens or crowd each other.

usage: python3 gate.py PLAN VOICEDIR
Each line needs: speech end (last word end) <= window_end, and start >= previous speech end + 0.8 s.
"""
import json
import sys
from pathlib import Path

plan, vdir = Path(sys.argv[1]), Path(sys.argv[2])
bad, prev_end, prev_id = 0, -1.0, None
for row in plan.read_text().splitlines():
    if not row[:1].isalpha() or row.startswith("id|"):
        continue
    lid, start, win, text = row.split("|", 3)
    start, win = float(start), float(win)
    w = json.loads((vdir / f"{lid}.words.json").read_text())
    speech = w[-1]["t"] + w[-1]["d"]
    end = start + speech
    msgs = []
    if end > win:
        msgs.append(f"overruns its screen by {end - win:.2f}s")
    if prev_end >= 0 and start - prev_end < 0.8:
        msgs.append(f"starts {start - prev_end:.2f}s after {prev_id}")
    flag = "FAIL" if msgs else "ok  "
    bad += bool(msgs)
    print(f"{flag} {lid} {start:7.2f} -> {end:7.2f} (window {win:7.2f}, slack {win - end:5.2f}) {'; '.join(msgs)}")
    prev_end, prev_id = end, lid
print("refused" if bad else "passed", f"({bad} failing)")
sys.exit(1 if bad else 0)
