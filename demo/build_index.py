"""Write intro/index.html from intro/timing.json: one host per scene, one <audio> per narration line."""
import json
from pathlib import Path

ROOT = Path(__file__).parent / "intro"
T = json.loads((ROOT / "timing.json").read_text())
hosts = "\n".join(
    f'      <div id="{k}" class="clip" data-composition-id="{k}" data-composition-src="compositions/{k}.html" '
    f'data-start="{v["start"]}" data-duration="{round(v["end"] - v["start"], 2)}" data-track-index="1" data-track-kind="graphics" '
    f'data-width="1920" data-height="1080"></div>'
    for k, v in T["scenes"].items()
)
# The voice is mixed by ffmpeg at assembly (this machine has no ffprobe for HyperFrames' own mixer).
audio = "\n".join(
    f'      <audio id="vo-{ln["id"]}" class="clip" src="assets/voice/{ln["id"]}.mp3" data-start="{ln["start"]}" data-duration="{round(ln["end"] - ln["start"] + 0.35, 2)}" data-track-index="{2 + i % 2}" data-volume="1"></audio>'
    for i, ln in enumerate(T["lines"])
)
html = f"""<!doctype html>
<html lang="en" data-resolution="landscape">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=1920, height=1080" />
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
    <style>
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{ margin: 0; width: 1920px; height: 1080px; overflow: hidden; background: #ffffff; }}
      #root {{ position: relative; width: 100%; height: 100%; background: #ffffff; }}
      #ground {{ position: absolute; inset: 0; background: #ffffff; }}
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="{T['total']}" data-width="1920" data-height="1080">
      <div id="ground"></div>
{hosts}
{audio}
    </div>
    <script>
      const tl = gsap.timeline({{ paused: true }});
      window.__timelines["main"] = tl;
    </script>
  </body>
</html>
"""
(ROOT / "index.html").write_text(html)
print("index.html", T["total"], "s,", len(T["scenes"]), "scenes,", len(T["lines"]), "lines")
