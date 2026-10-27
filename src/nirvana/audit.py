"""The overnight audit: one job that does the whole migration check while nobody watches.

1. Run the full workload against the chosen Zetaris copy.
2. Hand every blocked table and every query kept on Databricks to the investigating agent.
3. Re-judge the blocked queries with whatever the agent proved.
4. Install a Zetaris guard rule for every column the agent repaired.
5. Write a morning report: what can move, what moved only because of the agent, what stays.

Every model call in step 2 goes through Meterless Nightshift, which keeps the conversation as
one of its jobs; every finding is checked on the engines before it counts.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class Phase:
    name: str
    status: str = "waiting"  # waiting | running | done | skipped | failed
    detail: str = ""
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class Audit:
    id: str
    target_location: str
    status: str = "running"
    phases: list[Phase] = field(default_factory=list)
    first_run: str | None = None
    rejudge_run: str | None = None
    investigations: list[str] = field(default_factory=list)
    guards: list[dict[str, Any]] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    started_at: float = 0.0
    finished_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


PHASES = ["Run all queries", "Investigate what was blocked or kept", "Re-judge with the proven findings", "Install guard rules", "Morning report"]


def morning_report(first: dict[str, Any], rejudge: dict[str, Any] | None, invs: list[dict[str, Any]], guards: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts only from recorded runs and investigations."""
    fs = first["summary"]
    n = fs["queries"]
    before = fs["movable"]
    unblocked = (rejudge or {}).get("summary", {}).get("movable", 0) if rejudge else 0
    fixed = sum(1 for i in invs if i["kind"] == "query" and i.get("cleared"))
    tables = [i for i in invs if i["kind"] == "table"]
    return {
        "queries": n,
        "movable_before": before,
        "movable_after": before + unblocked + fixed,
        "unblocked_by_agent": unblocked,
        "fixed_by_agent": fixed,
        "tables_blocked": len(tables),
        "tables_cleared": sum(1 for i in tables if i.get("cleared")),
        "queries_kept": sum(1 for i in invs if i["kind"] == "query"),
        "repairs": [{"table": i["subject"], "column": f["column"], "expression": f["expression"]}
                    for i in tables for f in i.get("findings", []) if f["kind"] == "repair"],
        "same_values": [{"table": i["subject"], "column": f["column"]} for i in tables for f in i.get("findings", []) if f["kind"] == "same_values"],
        "guards": [{"table": g["table"], "rule": r["name"], "breaking": r["invalid"], "rows": r["total"]} for g in guards for r in g.get("rules", [])],
        "model_steps": sum(len(i.get("steps", [])) for i in invs),
        "still_open": [i["subject"] for i in invs if not i.get("cleared")],
    }


class Auditor:
    """Wires the audit to the server's runs, agent and guard builder through callables, so the
    sequence is testable without engines."""

    def __init__(
        self,
        *,
        run_queries: Callable[[list[str] | None, list[str]], str],
        run_record: Callable[[str], dict[str, Any]],
        alignment: Callable[[str], list[dict[str, Any]]],
        judgements: Callable[[str], list[dict[str, Any]]],
        investigate: Callable[[str, str | None, str | None], dict[str, Any]],
        guard: Callable[[str, str], dict[str, Any]],
        store: "AuditStore",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.run_queries, self.run_record, self.alignment, self.judgements = run_queries, run_record, alignment, judgements
        self.investigate, self.guard, self.store, self.clock = investigate, guard, store, clock

    def start(self, target_location: str, queries: list[str] | None = None) -> Audit:
        a = Audit(uuid.uuid4().hex[:10], target_location, phases=[Phase(p) for p in PHASES], started_at=self.clock())
        self.store.put(a)
        threading.Thread(target=self.execute, args=(a, queries), daemon=True).start()
        return a

    def _phase(self, a: Audit, i: int, status: str, detail: str = "") -> None:
        p = a.phases[i]
        if status == "running":
            p.started_at = self.clock()
        elif status in ("done", "skipped", "failed"):
            p.finished_at = self.clock()
        p.status, p.detail = status, detail or p.detail
        self.store.put(a)

    def execute(self, a: Audit, queries: list[str] | None = None) -> Audit:
        try:
            self._phase(a, 0, "running", "Databricks and Zetaris")
            a.first_run = self.run_queries(queries, [])
            first = self.run_record(a.first_run)
            if first.get("status") != "done":
                raise RuntimeError(f"the first run ended {first.get('status')}: {first.get('error')}")
            s = first["summary"]
            self._phase(a, 0, "done", f"{s['movable']} of {s['queries']} can move")

            blocked_tables = [x["table"] for x in self.alignment(a.first_run) if x["status"] == "not_aligned"]
            js = self.judgements(a.first_run)
            kept = [j["query_id"] for j in js if j["verdict"] == "keep"]
            self._phase(a, 1, "running", f"{len(blocked_tables)} tables, {len(kept)} queries")
            invs: list[dict[str, Any]] = []
            for t in blocked_tables:
                inv = self.investigate(a.first_run, t, None)
                invs.append(inv)
                a.investigations.append(inv["id"])
                self.store.put(a)
            for q in kept:
                inv = self.investigate(a.first_run, None, q)
                invs.append(inv)
                a.investigations.append(inv["id"])
                self.store.put(a)
            cleared = [i for i in invs if i["kind"] == "table" and i.get("cleared")]
            self._phase(a, 1, "done", f"{len(cleared)} of {len(blocked_tables)} tables cleared, "
                                      f"{sum(1 for i in invs if i['kind'] == 'query' and i.get('cleared'))} of {len(kept)} queries fixed")

            blocked_q = [j["query_id"] for j in js if j["verdict"] == "blocked" and j.get("cause") == "input_not_aligned"]
            rejudge = None
            if cleared and blocked_q:
                self._phase(a, 2, "running", f"{len(blocked_q)} queries")
                a.rejudge_run = self.run_queries(blocked_q, [i["id"] for i in cleared])
                rejudge = self.run_record(a.rejudge_run)
                rs = rejudge.get("summary") or {}
                self._phase(a, 2, "done", f"{rs.get('movable', 0)} of {len(blocked_q)} now move")
            else:
                self._phase(a, 2, "skipped", "nothing was cleared")

            repairs = sorted({(i["subject"], f["column"]) for i in cleared for f in i.get("findings", []) if f["kind"] == "repair"})
            if repairs:
                self._phase(a, 3, "running", f"{len(repairs)} columns")
                for t, c in repairs:
                    g = self.guard(t, c)
                    a.guards.append(g)
                    self.store.put(a)
                broken = sum(1 for g in a.guards for r in g.get("rules", []) if r.get("invalid"))
                self._phase(a, 3, "done", f"{len(a.guards)} installed on Zetaris, {broken} currently broken")
            else:
                self._phase(a, 3, "skipped", "no repairs to guard")

            self._phase(a, 4, "running")
            a.report = morning_report(first, rejudge, invs, a.guards)
            self._phase(a, 4, "done", f"{a.report['movable_before']} → {a.report['movable_after']} of {a.report['queries']} can move")
            a.status = "done"
        except Exception as e:  # recorded on the audit, never raised into the server
            a.status, a.error = "failed", f"{type(e).__name__}: {e}"[:400]
            for p in a.phases:
                if p.status == "running":
                    p.status, p.finished_at = "failed", self.clock()
        a.finished_at = self.clock()
        self.store.put(a)
        return a


class AuditStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.live: dict[str, Audit] = {}
        self.lock = threading.Lock()

    def put(self, a: Audit) -> None:
        with self.lock:
            self.live[a.id] = a
            if a.status != "running":
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a") as f:
                    f.write(json.dumps(a.as_dict(), default=str) + "\n")

    def all(self) -> list[dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                try:
                    x = json.loads(line)
                    out[x["id"]] = x
                except (json.JSONDecodeError, KeyError):
                    continue
        with self.lock:
            for k, v in self.live.items():
                out[k] = v.as_dict()
        return sorted(out.values(), key=lambda x: -(x.get("started_at") or 0))

    def get(self, audit_id: str) -> dict[str, Any] | None:
        return next((x for x in self.all() if x["id"] == audit_id), None)
