"""Lay the narration on a timeline: each line after the previous with a gap, a longer gap at scene changes.
Prints scene windows and the absolute time of chosen anchor words; writes timing.json."""
import json
from pathlib import Path

ROOT = Path(__file__).parent / "intro"
LEAD, GAP, SCENE_GAP, TAIL = 0.7, 0.55, 1.5, 1.1

rows = [r.split("|") for r in (ROOT / "NARRATION.txt").read_text().splitlines() if r[:1] == "L"]
t = LEAD
lines, scenes, prev_scene = [], {}, None
for lid, scene, text in rows:
    words = json.loads((ROOT / "assets/voice" / f"{lid}.words.json").read_text())
    speech = words[-1]["t"] + words[-1]["d"]
    if prev_scene is not None and scene != prev_scene:
        t += SCENE_GAP - GAP
        scenes[prev_scene]["end"] = round(t - 0.45, 2)
    if scene not in scenes:
        scenes[scene] = {"start": round(t - (LEAD if not scenes else 0.45), 2)}
    lines.append({"id": lid, "scene": scene, "start": round(t, 2), "end": round(t + speech, 2), "words": [{"w": w["w"], "t": round(t + w["t"], 2)} for w in words]})
    t += speech + GAP
    prev_scene = scene
scenes[prev_scene]["end"] = round(t - GAP + TAIL, 2)
scenes["bill"]["start"] = 0.0
total = scenes[prev_scene]["end"]
(ROOT / "timing.json").write_text(json.dumps({"total": total, "scenes": scenes, "lines": lines}, indent=1))
print("total", total)
for k, v in scenes.items():
    print(f"{k:8s} {v['start']:7.2f} -> {v['end']:7.2f}  ({v['end'] - v['start']:.2f})")
for ln in lines:
    s0 = scenes[ln["scene"]]["start"]
    print(f"{ln['id']} {ln['scene']:7s} abs {ln['start']:6.2f}-{ln['end']:6.2f}  rel {ln['start'] - s0:6.2f}  " + " ".join(f"{w['w']}@{w['t'] - s0:.2f}" for w in ln["words"]))
