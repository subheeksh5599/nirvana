"""Synthesize each narration line with word timings.

usage: tts/bin/python words.py PLAN OUTDIR
Writes OUTDIR/<id>.mp3 and OUTDIR/<id>.words.json ([{"w": word, "t": seconds}]).
"""
import asyncio
import json
import sys
from pathlib import Path

import edge_tts

VOICE = "en-US-AndrewMultilingualNeural"


async def one(text: str, mp3: Path, words: Path) -> None:
    for attempt in range(4):
        out, marks = bytearray(), []
        try:
            comm = edge_tts.Communicate(text, VOICE, rate="+0%", boundary="WordBoundary")
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    out.extend(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    marks.append({"w": chunk["text"], "t": round(chunk["offset"] / 1e7, 3), "d": round(chunk["duration"] / 1e7, 3)})
        except Exception as e:  # retry: the service drops connections now and then
            print(f"retry {mp3.name}: {e}", file=sys.stderr)
            await asyncio.sleep(2)
            continue
        if out and marks:
            mp3.write_bytes(bytes(out))
            words.write_text(json.dumps(marks))
            return
    raise SystemExit(f"synthesis failed for {mp3.name}")


async def main() -> None:
    plan, outdir = Path(sys.argv[1]), Path(sys.argv[2])
    outdir.mkdir(parents=True, exist_ok=True)
    for row in plan.read_text().splitlines():
        if not row[:1].isalnum() or row.startswith("id|"):
            continue
        cols = row.split("|")
        lid, text = cols[0], cols[-1]
        stamp = outdir / f"{lid}.txt"
        mp3, words = outdir / f"{lid}.mp3", outdir / f"{lid}.words.json"
        if mp3.exists() and words.exists() and stamp.exists() and stamp.read_text() == text:
            continue
        for f in (mp3, words, stamp):
            f.unlink(missing_ok=True)
        await one(text, mp3, words)
        stamp.write_text(text)
    print("ok")


asyncio.run(main())
