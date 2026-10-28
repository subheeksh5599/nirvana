"""Write NARRATION.md: every spoken line with its time in the final file (and in the recording, for the capture)."""
import json
from pathlib import Path

D = Path(__file__).parent
T = json.loads((D / "intro/timing.json").read_text())
INTRO = 126.733  # measured length of work/intro.mp4


def mmss(s: float) -> str:
    return f"{int(s // 60)}:{s % 60:04.1f}"


out = ["# Nirvana demo: narration", "",
       "Voice: Microsoft `en-US-AndrewMultilingualNeural` through edge-tts, natural pace. Switching voice is the `VOICE` line in `words.py`.",
       "", f"Final file: intro {mmss(INTRO)} (HyperFrames, `intro/`), then the screen capture, unedited, with narration. Last frame held 1.5 s.", "",
       "## Intro (rendered)", "", "| Final time | Scene | Line |", "|---|---|---|"]
for ln in T["lines"]:
    text = next(r.split("|")[-1] for r in (D / "intro/NARRATION.txt").read_text().splitlines() if r.startswith(ln["id"] + "|"))
    out.append(f"| {mmss(ln['start'])} | {ln['scene']} | {text} |")
out += ["", "## Screen capture", "", "| Final time | Recording time | Line |", "|---|---|---|"]
for r in (D / "CAPTURE_NARRATION.txt").read_text().splitlines():
    if not r.startswith("C"):
        continue
    lid, start, _win, text = r.split("|", 3)
    out.append(f"| {mmss(INTRO + float(start))} | {mmss(float(start))} | {text} |")
out += ["", "Every line is gated by `gate.py`: it must end before its screen changes and start at least 0.8 s after the previous one.",
        "Nothing was cut from the recording. Numbers in the lines are read off the screen or the recorded runs."]
(D / "NARRATION.md").write_text("\n".join(out) + "\n")
print("NARRATION.md", len(out), "lines")
