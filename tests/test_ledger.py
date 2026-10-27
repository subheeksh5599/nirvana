import json

import pytest

from nirvana.ledger import Ledger, sha256_text, summarize


def judgement(qid, verdict="move", cause=None, **kw):
    d = {"query_id": qid, "verdict": verdict, "cause": cause, "note": "n", "source_sql": f"s {qid}", "target_sql": f"t {qid}",
         "comparison": {"source_fp": "a" * 64, "target_fp": "b" * 64}, "source_ms": 10.0, "target_ms": 5.0}
    d.update(kw)
    return d


@pytest.fixture
def led():
    l = Ledger(":memory:")
    l.create_run("r1", mode="live", source={"name": "databricks"}, target={"name": "zetaris"}, query_ids=["q1", "q2"], created_at=1000.0)
    return l


def test_run_lifecycle(led):
    r = led.run("r1")
    assert r["status"] == "running" and r["finished_at"] is None and r["recorded"] is False
    led.finish_run("r1", "done", {"queries": 2})
    r = led.run("r1")
    assert r["status"] == "done" and r["summary"] == {"queries": 2} and r["finished_at"] >= 1000.0


def test_missing_run(led):
    assert led.run("nope") is None
    assert led.receipt("nope") is None
    with pytest.raises(KeyError):
        led.export("nope")


def test_runs_newest_first(led):
    led.create_run("r2", mode="live", source={}, target={}, query_ids=[], created_at=2000.0)
    assert [r["id"] for r in led.runs()] == ["r2", "r1"]


def test_judgements_keep_position_order(led):
    led.put_judgement("r1", 1, judgement("q2"))
    led.put_judgement("r1", 0, judgement("q1"))
    assert [j["query_id"] for j in led.judgements("r1")] == ["q1", "q2"]
    assert led.judgement("r1", "q2")["query_id"] == "q2"
    assert led.judgement("r1", "q9") is None


def test_judgement_replace(led):
    led.put_judgement("r1", 0, judgement("q1", "keep"))
    led.put_judgement("r1", 0, judgement("q1", "move"))
    assert len(led.judgements("r1")) == 1 and led.judgements("r1")[0]["verdict"] == "move"


def test_alignment_sorted(led):
    led.put_alignment("r1", {"table": "store", "status": "aligned"})
    led.put_alignment("r1", {"table": "item", "status": "aligned_types"})
    assert [a["table"] for a in led.alignment("r1")] == ["item", "store"]


def test_events_sequence(led):
    assert led.add_event("r1", "started", {"x": 1}) == 1
    assert led.add_event("r1", "judged", {"q": "q1"}) == 2
    assert [e["seq"] for e in led.events("r1")] == [1, 2]
    assert led.events("r1", after=1)[0]["kind"] == "judged"
    assert led.events("other") == []


def test_receipt_hash_changes_with_any_verdict(led):
    led.put_judgement("r1", 0, judgement("q1", "move"))
    a = led.receipt("r1")["sha256"]
    led.put_judgement("r1", 0, judgement("q1", "keep", "unexplained"))
    b = led.receipt("r1")["sha256"]
    assert a != b


def test_receipt_hash_changes_with_fingerprint(led):
    led.put_judgement("r1", 0, judgement("q1"))
    a = led.receipt("r1")["sha256"]
    led.put_judgement("r1", 0, judgement("q1", comparison={"source_fp": "c" * 64, "target_fp": "b" * 64}))
    assert led.receipt("r1")["sha256"] != a


def test_receipt_items(led):
    led.put_judgement("r1", 0, judgement("q1", "rewrite", "integer_division", rewritten_sql="SELECT 1", final_comparison={"target_fp": "a" * 64}))
    item = led.receipt("r1")["receipt"]["items"][0]
    assert item["rewrite_sha256"] == sha256_text("SELECT 1")
    assert item["source_sql_sha256"] == sha256_text("s q1")
    assert item["final_target_fp"] == "a" * 64


def test_export_import_round_trip(led, tmp_path):
    led.put_alignment("r1", {"table": "item", "status": "aligned", "source_rows": 5, "target_rows": 5})
    led.put_judgement("r1", 0, judgement("q1"))
    led.add_event("r1", "started", {})
    led.finish_run("r1", "done", {"queries": 1})
    rec = json.loads(json.dumps(led.export("r1")))
    other = Ledger(tmp_path / "o.sqlite")
    assert other.import_recording(rec) == "r1"
    assert other.run("r1")["recorded"] is True
    assert other.judgements("r1") == led.judgements("r1")
    assert other.receipt("r1")["sha256"] == led.receipt("r1")["sha256"]
    assert other.import_recording(rec) == "r1"  # idempotent


def test_import_rejects_tampered_recording(led):
    led.put_judgement("r1", 0, judgement("q1"))
    led.finish_run("r1", "done", {})
    rec = json.loads(json.dumps(led.export("r1")))
    rec["judgements"][0]["verdict"] = "move_note"
    with pytest.raises(ValueError, match="receipt hash"):
        Ledger(":memory:").import_recording(rec)


def test_import_rejects_other_formats():
    with pytest.raises(ValueError):
        Ledger(":memory:").import_recording({"format": "x"})


def test_summarize():
    js = [judgement("q1"), judgement("q2", "rewrite", "integer_division"), judgement("q3", "keep", "unexplained"), judgement("q4", "move_note", "float_noise"), judgement("q5", "blocked", "source_error")]
    al = [{"table": "a", "status": "aligned"}, {"table": "b", "status": "aligned_types"}, {"table": "c", "status": "not_aligned"}]
    s = summarize(js, al)
    assert s["queries"] == 5 and s["movable"] == 3 and s["movable_share"] == 0.6
    assert s["verdicts"] == {"move": 1, "rewrite": 1, "keep": 1, "move_note": 1, "blocked": 1}
    assert s["causes"] == {"integer_division": 1, "unexplained": 1, "float_noise": 1, "source_error": 1}
    assert (s["tables"], s["tables_usable"], s["tables_retyped"]) == (3, 2, 1)
    assert s["source_ms"] == 50.0 and s["target_ms"] == 25.0


def test_summarize_empty():
    s = summarize([], [])
    assert s["queries"] == 0 and s["movable_share"] == 0.0


def test_file_ledger_persists(tmp_path):
    p = tmp_path / "x" / "l.sqlite"
    a = Ledger(p)
    a.create_run("r", mode="live", source={}, target={}, query_ids=[])
    assert Ledger(p).run("r")["id"] == "r"
