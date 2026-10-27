import pytest

from nirvana.align import align_table, profile_sql, table_ref
from nirvana.engines import DuckDBEngine, rehearsal_pair
from nirvana.model import Column
from nirvana.workload import TPCDS_TABLES


@pytest.fixture(scope="module")
def pair():
    return rehearsal_pair(0.01)


@pytest.fixture(scope="module")
def tampered():
    src = DuckDBEngine.tpcds("tamper-source", 0.01)
    tgt = src.clone_with_types("tamper-target", schema="tampered")
    src.con.execute("DELETE FROM tampered.reason WHERE r_reason_sk = 1")
    src.con.execute("UPDATE tampered.store SET s_tax_percentage = s_tax_percentage + 0.01 WHERE s_store_sk = 1")
    src.con.execute("UPDATE tampered.ship_mode SET sm_carrier = sm_carrier || ' ' WHERE sm_ship_mode_sk = 1")
    src.con.execute("ALTER TABLE tampered.income_band DROP COLUMN ib_upper_bound")
    src.con.execute("DROP TABLE tampered.web_site")
    return src, tgt


@pytest.mark.parametrize("loc,table,expected", [("TPCDS_DB", "item", "TPCDS_DB.item"), ("samples.tpcds_sf1", "item", "samples.tpcds_sf1.item")])
def test_table_ref(loc, table, expected):
    assert table_ref(loc, table) == expected


def test_profile_sql_measures():
    sql, measures = profile_sql("s.t", [Column("a", "int"), Column("b", "string"), Column("c", "date"), Column("d", "array<int>")])
    assert measures == [
        "rows", "a.nonnull", "a.sum", "a.min", "a.max", "b.nonnull", "b.length", "b.min", "b.max", "c.nonnull", "c.min", "c.max", "d.nonnull",
    ]
    assert sql.startswith("SELECT COUNT(*) AS n_rows") and sql.endswith("FROM s.t")
    assert "CAST(`a` AS DECIMAL(38,0))" in sql


@pytest.mark.parametrize("table", sorted(TPCDS_TABLES))
def test_every_table_aligns_on_the_rehearsal_pair(pair, table):
    src, tgt = pair
    a = align_table(src, tgt, table)
    assert a.usable, a.as_dict()
    assert a.differences == []
    assert a.source_rows == a.target_rows


def test_decimal_tables_report_type_changes(pair):
    a = align_table(*pair, "store_sales")
    assert a.status == "aligned_types"
    changed = {c["column"] for c in a.type_changes}
    assert "ss_net_paid" in changed
    assert all(c["target"] == "double" for c in a.type_changes)


def test_integer_only_table_is_plainly_aligned(pair):
    # income_band has only integer columns, so no DECIMAL was retyped.
    assert align_table(*pair, "income_band").status == "aligned"


def test_missing_row_is_caught(tampered):
    a = align_table(*tampered, "reason")
    assert a.status == "not_aligned"
    assert a.source_rows == a.target_rows + 1
    assert any(d["measure"] == "rows" for d in a.differences)


def test_changed_value_is_caught(tampered):
    a = align_table(*tampered, "store")
    assert a.status == "not_aligned"
    assert {d["measure"] for d in a.differences} >= {"s_tax_percentage.sum"}


def test_trailing_space_is_caught(tampered):
    a = align_table(*tampered, "ship_mode")
    assert a.status == "not_aligned"
    assert any(d["measure"] == "sm_carrier.length" for d in a.differences)


def test_missing_column_is_caught(tampered):
    a = align_table(*tampered, "income_band")
    assert a.status == "not_aligned"
    assert "ib_upper_bound" in a.error


def test_missing_table_is_reported(tampered):
    a = align_table(*tampered, "web_site")
    assert a.status == "missing"
    assert not a.usable
    assert "tamper-target" in a.error


def test_untouched_table_still_aligned(tampered):
    assert align_table(*tampered, "warehouse").status == "aligned"


def test_as_dict_round_trip(pair):
    d = align_table(*pair, "item").as_dict()
    assert d["table"] == "item" and d["status"] in ("aligned", "aligned_types")
    assert "SELECT COUNT(*)" in d["source_sql"]


@pytest.mark.parametrize("typ,scale", [("int", 0), ("bigint", 0), ("decimal(7,2)", 2), ("decimal(15,4)", 4), ("decimal", 6), ("double", 6), ("real", 6)])
def test_measure_scale(typ, scale):
    from nirvana.align import measure_scale

    assert measure_scale(Column("x", typ)) == scale


def test_single_precision_copy_still_aligns_at_source_scale():
    src = DuckDBEngine.tpcds("f32-source", 0.01)
    tgt = src.clone_with_types("f32-target", decimal_as="REAL", schema="f32")
    a = align_table(src, tgt, "store_sales")
    assert a.usable, a.differences
    assert all(c["target"] == "float" for c in a.type_changes)
