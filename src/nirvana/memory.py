"""What Nirvana remembers between runs, kept in the Meterless H-MEM reference engine.

When a rewrite proves a cause, the fix is stored as a memory tagged with the plain facts of
the difference (``has_division``, ``status:different``, ``reads_retyped_columns``...). On the
next difference the judge asks which fixes match its facts and tries those first. Memory
only orders the attempts: every recalled fix is still run and compared, and a recalled fix
that fails gets ``not_helpful`` feedback, which lowers its standing in H-MEM's trust ledger.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "bridges" / "hmem.ts"
TSX = ROOT / "node_modules" / ".bin" / "tsx"


class MemoryUnavailable(RuntimeError):
    pass


class HMemMemory:
    def __init__(self, persist_dir: Path, *, tsx: Path = TSX, bridge: Path = BRIDGE, threshold: float = 0.05) -> None:
        if not tsx.exists():
            raise MemoryUnavailable("node_modules/.bin/tsx is missing; run `npm install` in the repository root")
        if shutil.which("node") is None:
            raise MemoryUnavailable("node is not installed")
        persist_dir.mkdir(parents=True, exist_ok=True)
        self.persist_dir = persist_dir
        self.threshold = threshold
        self._lock = threading.Lock()
        self._seq = 0
        self._recalled: dict[str, list[str]] = {}  # rewrite -> memory ids recalled for it
        self._proc = subprocess.Popen(
            [str(tsx), str(bridge), str(persist_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(ROOT),
        )
        first = self._proc.stdout.readline() if self._proc.stdout else ""
        if '"ready"' not in first:
            err = self._proc.stderr.read() if self._proc.stderr else ""
            raise MemoryUnavailable(f"the H-MEM bridge did not start: {err.strip()[:400]}")

    def _call(self, op: str, **payload: Any) -> Any:
        with self._lock:
            self._seq += 1
            req = {"id": self._seq, "op": op, **payload}
            assert self._proc.stdin and self._proc.stdout
            self._proc.stdin.write(json.dumps(req) + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
        if not line:
            raise MemoryUnavailable("the H-MEM bridge exited")
        resp = json.loads(line)
        if resp.get("error"):
            raise MemoryUnavailable(resp["error"])
        return resp["result"]

    # -- the judge's Memory protocol ------------------------------------------------

    def remember(self, features: list[str], rewrite: str, query_id: str) -> None:
        if rewrite.startswith("proof:"):
            content = f"{rewrite.split(':', 1)[1].replace('_', ' ')} proven for a difference with {', '.join(features)} (query {query_id})"
        else:
            content = f"rewrite {rewrite} proved the cause of a difference with {', '.join(features)} (query {query_id})"
        tags = [f"rewrite:{rewrite}", f"query:{query_id}", *features]
        self._call("add", content=content, tags=tags, source="nirvana-judge", confidence=0.9)
        for mid in self._recalled.pop(rewrite, []):
            self._call("feedback", memoryId=mid, kind="helpful")

    def recall(self, features: list[str]) -> list[str]:
        if not features:
            return []
        res = self._call("query", text=" ".join(features), topN=8, threshold=self.threshold)
        order: list[str] = []
        self._recalled = {}
        for m in res.get("memories", []):
            for t in m.get("tags", []):
                if t.startswith("rewrite:") and not t.startswith("rewrite:proof:"):
                    name = t.split(":", 1)[1]
                    self._recalled.setdefault(name, []).append(m["id"])
                    if name not in order:
                        order.append(name)
        return order

    def knows(self, features: list[str], proof: str) -> bool:
        """True when a stored memory names this proof for exactly these features."""
        if not features:
            return False
        res = self._call("query", text=" ".join(features), topN=8, threshold=self.threshold)
        want = set(features)
        for m in res.get("memories", []):
            tags = set(m.get("tags", []))
            if f"rewrite:{proof}" in tags and want <= tags and not any(t.startswith("status:") and t not in want for t in tags):
                self._call("feedback", memoryId=m["id"], kind="helpful")
                return True
        return False

    def remember_finding(self, text: str, tags: list[str]) -> None:
        """A finding the investigating agent proved, for the next investigation to start from."""
        self._call("add", content=text, tags=["kind:finding", *tags], source="nirvana-agent", confidence=0.9)

    def recall_findings(self, text: str, top: int = 5) -> list[dict[str, Any]]:
        res = self._call("query", text=text, topN=top * 3, threshold=self.threshold)
        out = []
        for m in res.get("memories", []):
            if "kind:finding" in m.get("tags", []):
                out.append({"id": m.get("id"), "content": m.get("content"), "tags": m.get("tags", []), "relevance": m.get("relevance")})
                self._call("feedback", memoryId=m["id"], kind="helpful")
        return out[:top]

    def forget_failed(self) -> None:
        """Recalled fixes that were tried and did not prove the cause lose trust."""
        for ids in self._recalled.values():
            for mid in ids:
                self._call("feedback", memoryId=mid, kind="not_helpful")
        self._recalled = {}

    def stats(self) -> dict[str, Any]:
        return {"engine": "meterless-hmem-reference", "persist_dir": str(self.persist_dir), **self._call("stats")}

    def search(self, text: str, top: int = 10) -> list[dict[str, Any]]:
        return self._call("query", text=text, topN=top, threshold=self.threshold).get("memories", [])

    def close(self) -> None:
        if self._proc.poll() is None:
            if self._proc.stdin:
                self._proc.stdin.close()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def __del__(self) -> None:  # best effort; the process also exits when stdin closes
        try:
            self.close()
        except Exception:
            pass
