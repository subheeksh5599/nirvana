"""The real H-MEM reference engine behind the bridge (needs node and `npm install`)."""

import shutil

import pytest

from nirvana.memory import TSX, HMemMemory, MemoryUnavailable

pytestmark = pytest.mark.skipif(shutil.which("node") is None or not TSX.exists(), reason="node or tsx missing")


@pytest.fixture
def mem(tmp_path):
    m = HMemMemory(tmp_path / "hmem")
    yield m
    m.close()


def test_empty_memory_recalls_nothing(mem):
    assert mem.recall(["has_division", "status:different"]) == []
    assert mem.recall([]) == []


def test_remembered_fix_is_recalled_for_the_same_facts(mem):
    mem.remember(["has_division", "status:different"], "cast_divisions_to_double", "q90")
    assert mem.recall(["has_division", "status:different"]) == ["cast_divisions_to_double"]


def test_recall_orders_by_relevance(mem):
    mem.remember(["reads_retyped_columns", "status:different", "output_type_changed"], "cast_drifted_columns", "q1")
    mem.remember(["has_division", "status:different"], "cast_divisions_to_double", "q2")
    got = mem.recall(["reads_retyped_columns", "output_type_changed"])
    assert got[0] == "cast_drifted_columns"


def test_memory_persists_across_processes(tmp_path):
    a = HMemMemory(tmp_path / "p")
    a.remember(["status:order_only"], "explicit_null_order", "q5")
    a.close()
    b = HMemMemory(tmp_path / "p")
    try:
        assert b.recall(["status:order_only"]) == ["explicit_null_order"]
        assert b.stats()["memories"] == 1
        assert (tmp_path / "p" / "state.json").exists()
    finally:
        b.close()


def test_feedback_paths_run(mem):
    mem.remember(["has_division"], "cast_divisions_to_double", "q1")
    mem.recall(["has_division"])
    mem.forget_failed()
    mem.recall(["has_division"])
    mem.remember(["has_division"], "cast_divisions_to_double", "q2")
    assert mem.stats()["memories"] == 2


def test_search_returns_content(mem):
    mem.remember(["has_division"], "cast_divisions_to_double", "q7")
    hits = mem.search("has_division")
    assert hits and "q7" in hits[0]["content"]
    assert "rewrite:cast_divisions_to_double" in hits[0]["tags"]


def test_missing_tsx_is_reported(tmp_path):
    with pytest.raises(MemoryUnavailable, match="npm install"):
        HMemMemory(tmp_path / "x", tsx=tmp_path / "nope")


def test_judge_with_real_memory(tmp_path):
    import duckdb

    from nirvana.engines import DuckDBEngine
    from nirvana.judge import Judge
    from nirvana.workload import Query

    con = duckdb.connect()
    con.execute("CREATE TABLE date_dim AS SELECT range AS d_moy FROM range(1, 13)")
    src = DuckDBEngine("s", "main", connection=con.cursor())
    cur = con.cursor()
    cur.execute("SET SESSION integer_division = true")
    tgt = DuckDBEngine("t", "main", connection=cur)
    m = HMemMemory(tmp_path / "j")
    try:
        sql = "SELECT d_moy, CAST(d_moy AS INT) / CAST(2 AS INT) AS h FROM date_dim ORDER BY d_moy"
        first = Judge(src, tgt, memory=m).judge(Query("a", sql))
        assert first.cause == "integer_division"
        second = Judge(src, tgt, memory=m).judge(Query("b", sql))
        tried = [s.name for s in second.steps if s.name.startswith("rewrite")]
        assert tried[0] == "rewrite: cast_divisions_to_double"
        assert m.stats()["memories"] == 2
    finally:
        m.close()


def test_order_ties_proof_is_known_only_for_the_same_features(mem):
    mem.remember(["status:order_only"], "proof:order_ties", "q6")
    assert mem.knows(["status:order_only"], "proof:order_ties") is True
    assert mem.knows(["status:different", "has_division"], "proof:order_ties") is False
    assert mem.knows([], "proof:order_ties") is False


def test_a_proof_is_never_recalled_as_a_rewrite(mem):
    mem.remember(["status:order_only"], "proof:order_ties", "q6")
    assert mem.recall(["status:order_only"]) == []


def test_proof_memory_reads_as_a_sentence(mem):
    mem.remember(["status:order_only"], "proof:order_ties", "q77")
    hits = mem.search("order ties")
    assert any("order ties proven" in h["content"] and "q77" in h["content"] for h in hits)
