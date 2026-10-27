"""The ledger: every run, alignment, judgement and event, in SQLite.

A run can be exported as one JSON recording (``recordings/<run_id>.json``) and imported
again, so a finished real run can be served when the engines are not running. The receipt
hash covers what a reviewer needs to check a run: the query texts, the fingerprints of both
engines' answers, the verdicts and any rewrite text.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  finished_at REAL,
  status TEXT NOT NULL,
  mode TEXT NOT NULL,
  source TEXT NOT NULL,
  target TEXT NOT NULL,
  query_ids TEXT NOT NULL,
  summary TEXT,
  error TEXT,
  recorded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS alignments (
  run_id TEXT NOT NULL,
  table_name TEXT NOT NULL,
  status TEXT NOT NULL,
  data TEXT NOT NULL,
  PRIMARY KEY (run_id, table_name)
);
CREATE TABLE IF NOT EXISTS judgements (
  run_id TEXT NOT NULL,
  query_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  verdict TEXT NOT NULL,
  cause TEXT,
  data TEXT NOT NULL,
  PRIMARY KEY (run_id, query_id)
);
CREATE TABLE IF NOT EXISTS events (
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  at REAL NOT NULL,
  kind TEXT NOT NULL,
  data TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);
"""

MOVABLE = ("move", "move_note", "rewrite")


def _dump(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, tuple(args))
            self._db.commit()
            return cur

    def _all(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchall()

    # ---------------------------------------------------------------- runs

    def create_run(self, run_id: str, *, mode: str, source: dict, target: dict, query_ids: list[str], created_at: float | None = None) -> None:
        self._exec(
            "INSERT INTO runs (id, created_at, status, mode, source, target, query_ids) VALUES (?, ?, 'running', ?, ?, ?, ?)",
            (run_id, created_at or time.time(), mode, _dump(source), _dump(target), _dump(query_ids)),
        )

    def finish_run(self, run_id: str, status: str, summary: dict | None = None, error: str | None = None) -> None:
        self._exec(
            "UPDATE runs SET status = ?, finished_at = ?, summary = ?, error = ? WHERE id = ?",
            (status, time.time(), _dump(summary) if summary is not None else None, error, run_id),
        )

    def run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._all("SELECT * FROM runs WHERE id = ?", (run_id,))
        return self._run_dict(rows[0]) if rows else None

    def runs(self) -> list[dict[str, Any]]:
        return [self._run_dict(r) for r in self._all("SELECT * FROM runs ORDER BY created_at DESC")]

    @staticmethod
    def _run_dict(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": r["id"],
            "created_at": r["created_at"],
            "finished_at": r["finished_at"],
            "status": r["status"],
            "mode": r["mode"],
            "source": json.loads(r["source"]),
            "target": json.loads(r["target"]),
            "query_ids": json.loads(r["query_ids"]),
            "summary": json.loads(r["summary"]) if r["summary"] else None,
            "error": r["error"],
            "recorded": bool(r["recorded"]),
        }

    # ---------------------------------------------------------------- parts

    def put_alignment(self, run_id: str, data: dict[str, Any]) -> None:
        self._exec(
            "INSERT OR REPLACE INTO alignments (run_id, table_name, status, data) VALUES (?, ?, ?, ?)",
            (run_id, data["table"], data["status"], _dump(data)),
        )

    def alignment(self, run_id: str) -> list[dict[str, Any]]:
        return [json.loads(r["data"]) for r in self._all("SELECT data FROM alignments WHERE run_id = ? ORDER BY table_name", (run_id,))]

    def put_judgement(self, run_id: str, position: int, data: dict[str, Any]) -> None:
        self._exec(
            "INSERT OR REPLACE INTO judgements (run_id, query_id, position, verdict, cause, data) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, data["query_id"], position, data["verdict"], data.get("cause"), _dump(data)),
        )

    def judgements(self, run_id: str) -> list[dict[str, Any]]:
        return [json.loads(r["data"]) for r in self._all("SELECT data FROM judgements WHERE run_id = ? ORDER BY position", (run_id,))]

    def judgement(self, run_id: str, query_id: str) -> dict[str, Any] | None:
        rows = self._all("SELECT data FROM judgements WHERE run_id = ? AND query_id = ?", (run_id, query_id))
        return json.loads(rows[0]["data"]) if rows else None

    def add_event(self, run_id: str, kind: str, data: dict[str, Any]) -> int:
        with self._lock:
            row = self._db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?", (run_id,)).fetchone()
            seq = int(row[0])
            self._db.execute("INSERT INTO events (run_id, seq, at, kind, data) VALUES (?, ?, ?, ?, ?)", (run_id, seq, time.time(), kind, _dump(data)))
            self._db.commit()
        return seq

    def events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        return [
            {"seq": r["seq"], "at": r["at"], "kind": r["kind"], "data": json.loads(r["data"])}
            for r in self._all("SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq", (run_id, after))
        ]

    # ---------------------------------------------------------------- receipt / recording

    def receipt(self, run_id: str) -> dict[str, Any] | None:
        run = self.run(run_id)
        if run is None:
            return None
        items = []
        for j in self.judgements(run_id):
            cmp = j.get("comparison") or {}
            items.append(
                {
                    "query_id": j["query_id"],
                    "source_sql_sha256": sha256_text(j["source_sql"]),
                    "target_sql_sha256": sha256_text(j["target_sql"]),
                    "verdict": j["verdict"],
                    "cause": j.get("cause"),
                    "source_fp": cmp.get("source_fp"),
                    "target_fp": cmp.get("target_fp"),
                    "rewrite_sha256": sha256_text(j["rewritten_sql"]) if j.get("rewritten_sql") else None,
                    "final_target_fp": (j.get("final_comparison") or {}).get("target_fp"),
                }
            )
        body = {
            "run_id": run_id,
            "mode": run["mode"],
            "source": run["source"],
            "target": run["target"],
            "created_at": run["created_at"],
            "finished_at": run["finished_at"],
            "alignment": [{"table": a["table"], "status": a["status"], "source_rows": a.get("source_rows"), "target_rows": a.get("target_rows")} for a in self.alignment(run_id)],
            "items": items,
        }
        return {"receipt": body, "sha256": sha256_text(_dump(body))}

    def export(self, run_id: str) -> dict[str, Any]:
        run = self.run(run_id)
        if run is None:
            raise KeyError(run_id)
        return {
            "format": "nirvana-recording/1",
            "run": run,
            "alignment": self.alignment(run_id),
            "judgements": self.judgements(run_id),
            "events": self.events(run_id),
            "receipt": self.receipt(run_id),
        }

    def import_recording(self, rec: dict[str, Any]) -> str:
        if rec.get("format") != "nirvana-recording/1":
            raise ValueError("not a nirvana recording")
        run = rec["run"]
        rid = run["id"]
        if self.run(rid) is not None:
            return rid
        self._exec(
            "INSERT INTO runs (id, created_at, finished_at, status, mode, source, target, query_ids, summary, error, recorded) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (rid, run["created_at"], run["finished_at"], run["status"], run["mode"], _dump(run["source"]), _dump(run["target"]), _dump(run["query_ids"]), _dump(run["summary"]) if run.get("summary") else None, run.get("error")),
        )
        for a in rec.get("alignment", []):
            self.put_alignment(rid, a)
        for i, j in enumerate(rec.get("judgements", [])):
            self.put_judgement(rid, i, j)
        with self._lock:
            for e in rec.get("events", []):
                self._db.execute("INSERT OR IGNORE INTO events (run_id, seq, at, kind, data) VALUES (?, ?, ?, ?, ?)", (rid, e["seq"], e["at"], e["kind"], _dump(e["data"])))
            self._db.commit()
        stored = self.receipt(rid)
        claimed = (rec.get("receipt") or {}).get("sha256")
        if claimed and stored and stored["sha256"] != claimed:
            raise ValueError(f"recording {rid} does not match its receipt hash")
        return rid


def summarize(judgements: list[dict[str, Any]], alignment: list[dict[str, Any]]) -> dict[str, Any]:
    from collections import Counter

    verdicts = Counter(j["verdict"] for j in judgements)
    causes = Counter(j["cause"] for j in judgements if j.get("cause"))
    total = len(judgements)
    movable = sum(verdicts.get(v, 0) for v in MOVABLE)
    src_ms = sum(j.get("source_ms") or 0 for j in judgements)
    tgt_ms = sum(j.get("target_ms") or 0 for j in judgements)
    return {
        "queries": total,
        "verdicts": dict(verdicts),
        "causes": dict(causes),
        "movable": movable,
        "movable_share": (movable / total) if total else 0.0,
        "tables": len(alignment),
        "tables_usable": sum(1 for a in alignment if a["status"] in ("aligned", "aligned_types")),
        "tables_retyped": sum(1 for a in alignment if a["status"] == "aligned_types"),
        "source_ms": round(src_ms, 1),
        "target_ms": round(tgt_ms, 1),
    }
