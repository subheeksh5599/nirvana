"""The overnight audit, sequenced against fakes: what it runs, in what order, and the report."""

import time

import pytest

from nirvana.audit import PHASES, Audit, Auditor, AuditStore, Phase, morning_report


def first_run(movable=47):
    return {"status": "done", "summary": {"queries": 103, "movable": movable}}


class World:
    def __init__(self, cleared=True, fixed=True, repairs=True):
        self.calls = []
        self.cleared, self.fixed, self.repairs = cleared, fixed, repairs

    def run_queries(self, queries, explained):
        self.calls.append(("run", tuple(queries or []), tuple(explained)))
        return "r1" if not explained else "r2"

    def run_record(self, rid):
        return first_run() if rid == "r1" else {"status": "done", "summary": {"queries": 2, "movable": 2}}

    def alignment(self, rid):
        return [{"table": "addr", "status": "not_aligned"}, {"table": "item", "status": "aligned"}]

    def judgements(self, rid):
        return [{"query_id": "q1", "verdict": "blocked", "cause": "input_not_aligned"},
                {"query_id": "q2", "verdict": "blocked", "cause": "input_not_aligned"},
                {"query_id": "q3", "verdict": "keep", "cause": "unexplained"},
                {"query_id": "q4", "verdict": "blocked", "cause": "source_error"},
                {"query_id": "q5", "verdict": "move", "cause": None}]

    def investigate(self, rid, table, query):
        self.calls.append(("investigate", table or query))
        if table:
            fs = [{"column": "zip", "kind": "repair", "expression": "lpad(cast(x as string), 5, '0')"}] if self.repairs else \
                 [{"column": "num", "kind": "same_values", "expression": None}]
            return {"id": "i-" + table, "kind": "table", "subject": table, "cleared": self.cleared, "findings": fs, "steps": [1, 2, 3]}
        return {"id": "i-" + query, "kind": "query", "subject": query, "cleared": self.fixed, "findings": [], "steps": [1]}

    def guard(self, table, column):
        self.calls.append(("guard", table, column))
        return {"table": table, "rules": [{"name": column + "_shape", "invalid": 1351, "total": 50000}]}


def auditor(w, tmp_path):
    return Auditor(run_queries=w.run_queries, run_record=w.run_record, alignment=w.alignment, judgements=w.judgements,
                   investigate=w.investigate, guard=w.guard, store=AuditStore(tmp_path / "a.jsonl"))


def test_the_full_sequence(tmp_path):
    w = World()
    a = auditor(w, tmp_path).execute(Audit("a1", "CSV", phases=[Phase(p) for p in PHASES]))
    assert a.status == "done", a.error
    assert w.calls == [
        ("run", (), ()),
        ("investigate", "addr"),
        ("investigate", "q3"),
        ("run", ("q1", "q2"), ("i-addr",)),
        ("guard", "addr", "zip"),
    ]
    assert [p.status for p in a.phases] == ["done"] * 5
    r = a.report
    assert (r["movable_before"], r["movable_after"], r["unblocked_by_agent"], r["fixed_by_agent"]) == (47, 50, 2, 1)
    assert r["repairs"] == [{"table": "addr", "column": "zip", "expression": "lpad(cast(x as string), 5, '0')"}]
    assert r["guards"] == [{"table": "addr", "rule": "zip_shape", "breaking": 1351, "rows": 50000}]
    assert r["model_steps"] == 4 and r["still_open"] == [] and r["staying"] == 53


def test_nothing_cleared_skips_the_rejudge(tmp_path):
    w = World(cleared=False, fixed=False)
    a = auditor(w, tmp_path).execute(Audit("a2", "CSV", phases=[Phase(p) for p in PHASES]))
    assert a.status == "done"
    assert [p.status for p in a.phases] == ["done", "done", "skipped", "skipped", "done"]
    assert a.report["movable_after"] == 47 and set(a.report["still_open"]) == {"addr", "q3"}


def test_same_values_need_no_guard(tmp_path):
    w = World(repairs=False)
    a = auditor(w, tmp_path).execute(Audit("a3", "CSV", phases=[Phase(p) for p in PHASES]))
    assert not any(c[0] == "guard" for c in w.calls) and a.phases[3].status == "skipped"
    assert a.report["same_values"] == [{"table": "addr", "column": "num"}]


def test_a_failed_first_run_fails_the_audit(tmp_path):
    w = World()
    w.run_record = lambda rid: {"status": "failed", "error": "target down"}
    a = auditor(w, tmp_path).execute(Audit("a4", "CSV", phases=[Phase(p) for p in PHASES]))
    assert a.status == "failed" and "target down" in a.error and a.phases[0].status == "failed"


def test_start_runs_in_the_background_and_is_stored(tmp_path):
    w = World()
    au = auditor(w, tmp_path)
    a = au.start("CSV")
    for _ in range(100):
        got = au.store.get(a.id)
        if got and got["status"] != "running":
            break
        time.sleep(0.02)
    assert au.store.get(a.id)["status"] == "done"
    assert AuditStore(tmp_path / "a.jsonl").get(a.id)["report"]["movable_after"] == 50


def test_report_counts_only_recorded_things():
    r = morning_report(first_run(10), None, [], [])
    assert r == {**r, "movable_before": 10, "movable_after": 10, "unblocked_by_agent": 0, "fixed_by_agent": 0, "tables_blocked": 0}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from nirvana.api import create_app
    from nirvana.config import settings

    env = {"NIRVANA_HOME": str(tmp_path), "NIRVANA_RECORDINGS": str(tmp_path / "rec")}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with TestClient(create_app(settings(env), mode="live")) as c:
        yield c


def test_api_lists_none(client):
    assert client.get("/api/audits").json() == []
    assert client.get("/api/audits/nope").status_code == 404


@pytest.mark.parametrize("body", [{"target_location": "a;b"}, {"target_location": "CSV", "queries": ["q999"]}])
def test_api_rejects_bad_requests(client, body):
    assert client.post("/api/audits", json=body).status_code == 400


def test_investigation_api_validation(client):
    assert client.post("/api/investigations", json={"run_id": "nope", "table": "store"}).status_code == 404
    assert client.get("/api/investigations").json() == []
