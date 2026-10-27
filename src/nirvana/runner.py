"""A run: check both engines, align the input tables, judge every query, record everything.

Runs execute in a background thread so the API can stream their events. A run can be
cancelled between queries.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .align import align_table
from .engines import Engine
from .judge import Advisor, Judge, Memory
from .ledger import Ledger, summarize
from .workload import Query, tables_read


def new_run_id(clock: Callable[[], float] = time.time) -> str:
    return time.strftime("run-%Y%m%d-%H%M%S", time.gmtime(clock())) + "-" + secrets.token_hex(3)


class Run:
    def __init__(
        self,
        ledger: Ledger,
        source: Engine,
        target: Engine,
        queries: list[Query],
        *,
        mode: str,
        advisor: Advisor | None = None,
        memory: Memory | None = None,
        max_rows: int = 100_000,
        run_id: str | None = None,
        recordings_dir: Path | None = None,
    ) -> None:
        self.ledger, self.source, self.target, self.queries = ledger, source, target, queries
        self.mode, self.advisor, self.memory, self.max_rows = mode, advisor, memory, max_rows
        self.id = run_id or new_run_id()
        self.recordings_dir = recordings_dir
        self._cancel = threading.Event()
        self.thread: threading.Thread | None = None
        ledger.create_run(self.id, mode=mode, source=source.describe(), target=target.describe(), query_ids=[q.id for q in queries])

    def cancel(self) -> None:
        self._cancel.set()

    def event(self, kind: str, **data: Any) -> None:
        self.ledger.add_event(self.id, kind, data)

    def start(self) -> "Run":
        self.thread = threading.Thread(target=self.execute, name=self.id, daemon=True)
        self.thread.start()
        return self

    def execute(self) -> dict[str, Any] | None:
        try:
            return self._execute()
        except Exception as e:  # recorded, never swallowed silently
            self.event("failed", error=f"{type(e).__name__}: {e}")
            self.ledger.finish_run(self.id, "failed", error=f"{type(e).__name__}: {e}")
            return None

    def _execute(self) -> dict[str, Any] | None:
        self.event("started", source=self.source.describe(), target=self.target.describe(), queries=len(self.queries))
        for eng, role in ((self.source, "source"), (self.target, "target")):
            st = eng.ping()
            self.event("ping", role=role, engine=st.engine, ok=st.ok, detail=st.detail)
            if not st.ok:
                self.ledger.finish_run(self.id, "failed", error=f"{role} engine {st.engine} is not reachable: {st.detail}")
                return None

        tables = sorted({t for q in self.queries for t in tables_read(q.sql)})
        alignment = {}
        for t in tables:
            if self._cancel.is_set():
                return self._cancelled()
            a = align_table(self.source, self.target, t)
            alignment[t] = a
            self.ledger.put_alignment(self.id, a.as_dict())
            self.event("aligned", table=t, status=a.status, source_rows=a.source_rows, target_rows=a.target_rows, type_changes=len(a.type_changes))

        judge = Judge(self.source, self.target, alignment=alignment, advisor=self.advisor, memory=self.memory, max_rows=self.max_rows)
        for i, q in enumerate(self.queries):
            if self._cancel.is_set():
                return self._cancelled()
            self.event("judging", query_id=q.id, position=i)
            j = judge.judge(q)
            self.ledger.put_judgement(self.id, i, j.as_dict())
            self.event("judged", query_id=q.id, verdict=j.verdict, cause=j.cause, note=j.note, position=i)

        summary = summarize(self.ledger.judgements(self.id), self.ledger.alignment(self.id))
        self.ledger.finish_run(self.id, "done", summary)
        self.event("finished", summary=summary)
        if self.recordings_dir is not None:
            self.save_recording()
        return summary

    def _cancelled(self) -> None:
        summary = summarize(self.ledger.judgements(self.id), self.ledger.alignment(self.id))
        self.ledger.finish_run(self.id, "cancelled", summary)
        self.event("cancelled", summary=summary)
        return None

    def save_recording(self) -> Path:
        assert self.recordings_dir is not None
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        path = self.recordings_dir / f"{self.id}.json"
        path.write_text(json.dumps(self.ledger.export(self.id), indent=1, sort_keys=True, default=str), encoding="utf-8")
        return path


def load_recordings(ledger: Ledger, folder: Path) -> list[str]:
    ids = []
    if not folder.is_dir():
        return ids
    for p in sorted(folder.glob("*.json")):
        ids.append(ledger.import_recording(json.loads(p.read_text(encoding="utf-8"))))
    return ids
