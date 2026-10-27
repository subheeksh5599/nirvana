import math
from decimal import Decimal

import pytest
from hypothesis import given, settings, strategies as st

from nirvana.compare import compare, numeric_gap
from nirvana.model import Column, ResultSet


def rs(rows, types=("int",), names=None, engine="e"):
    names = names or [f"c{i}" for i in range(len(types))]
    return ResultSet.build(engine, "q", [Column(n, t) for n, t in zip(names, types)], rows)


def test_identical_sets():
    c = compare(rs([(1,), (2,)]), rs([(1,), (2,)]))
    assert c.identical and c.status == "identical"
    assert c.source_fp == c.target_fp


def test_order_ignored_when_unordered():
    c = compare(rs([(1,), (2,)]), rs([(2,), (1,)]), ordered=False)
    assert c.identical


def test_order_only_when_ordered():
    c = compare(rs([(1,), (2,)]), rs([(2,), (1,)]), ordered=True)
    assert c.status == "order_only"
    assert not c.ordered_equal


def test_ordered_and_equal():
    c = compare(rs([(1,), (2,)]), rs([(1,), (2,)]), ordered=True)
    assert c.status == "identical" and c.ordered_equal


def test_scale_difference_is_not_a_difference():
    a = rs([(Decimal("1.50"),)], types=("decimal(7,2)",))
    b = rs([("1.5",)], types=("decimal(10,4)",))
    assert compare(a, b).identical


def test_integer_vs_decimal_same_value_identical_but_type_recorded():
    a = rs([(3,)], types=("int",))
    b = rs([(Decimal("3.00"),)], types=("decimal(10,2)",))
    c = compare(a, b)
    assert c.identical
    assert c.type_mismatches == [{"column": 0, "name": "c0", "source": "int", "target": "decimal(10,2)"}]


def test_changed_value_is_paired_with_column_named():
    a = rs([(1, "x"), (2, "y")], types=("int", "string"), names=["id", "v"])
    b = rs([(1, "x"), (2, "z")], types=("int", "string"), names=["id", "v"])
    c = compare(a, b)
    assert c.status == "different"
    assert c.only_in_source == c.only_in_target == 1
    assert c.mismatched_columns() == ["v"]
    cell = c.paired[0].cells[0]
    assert cell.source == ("str", "y") and cell.target == ("str", "z")
    assert cell.numeric_gap is None


def test_missing_row_is_unpaired():
    c = compare(rs([(1,), (2,)]), rs([(1,)]))
    assert c.status == "different"
    assert c.only_in_source == 1 and c.only_in_target == 0
    assert c.unpaired_source == [(("num", "2"),)]


def test_duplicates_count():
    c = compare(rs([(1,), (1,)]), rs([(1,)]))
    assert c.status == "different" and c.only_in_source == 1


def test_schema_mismatch():
    c = compare(rs([(1,)]), rs([(1, 2)], types=("int", "int")))
    assert c.status == "schema_mismatch"
    assert not c.column_count_equal


def test_float_noise_is_labelled_not_hidden():
    a = rs([(0.1 + 0.2,)], types=("double",))
    b = rs([(0.3,)], types=("double",))
    c = compare(a, b)
    assert c.status == "float_noise"
    assert not c.identical
    assert 0 < c.max_rel_gap < 1e-9


def test_float_gap_above_tolerance_is_different():
    a = rs([(1.0,)], types=("double",))
    b = rs([(1.001,)], types=("double",))
    assert compare(a, b).status == "different"


def test_custom_tolerance():
    a = rs([(1.0,)], types=("double",))
    b = rs([(1.001,)], types=("double",))
    assert compare(a, b, rel_tol=1e-2).status == "float_noise"


def test_decimal_vs_double_columns():
    a = rs([(Decimal("2.50"),)], types=("decimal(7,2)",))
    b = rs([(2.5,)], types=("double",))
    c = compare(a, b)
    assert c.status == "float_noise"  # values equal numerically but kinds differ
    assert c.max_rel_gap == 0.0
    assert c.type_mismatches[0]["target"] == "double"


def test_integer_division_shows_as_numeric_gap():
    a = rs([(Decimal("3.5"),)], types=("decimal(10,1)",))
    b = rs([(3,)], types=("int",))
    c = compare(a, b)
    assert c.status == "different"
    assert c.paired[0].cells[0].numeric_gap == 0.5


def test_null_vs_value_is_different():
    c = compare(rs([(None,)]), rs([(0,)]))
    assert c.status == "different"
    assert c.paired[0].cells[0].numeric_gap is None


def test_null_equals_null():
    assert compare(rs([(None,)]), rs([(None,)])).identical


def test_empty_results_identical():
    assert compare(rs([]), rs([])).identical


def test_pairing_prefers_most_equal_row():
    names = ["k", "a", "b"]
    types = ("int", "int", "int")
    a = rs([(1, 10, 100), (2, 20, 200)], types=types, names=names)
    b = rs([(2, 20, 201), (1, 10, 101)], types=types, names=names)
    c = compare(a, b)
    for p in c.paired:
        assert p.source[0] == p.target[0]
        assert [x.name for x in p.cells] == ["b"]


def test_summary_keys():
    s = compare(rs([(1,)]), rs([(2,)])).summary()
    assert set(s) == {
        "status", "source_rows", "target_rows", "source_fp", "target_fp", "ordered", "ordered_equal",
        "column_count_equal", "type_mismatches", "only_in_source", "only_in_target", "mismatched_columns", "max_rel_gap",
    }


def test_sample_is_capped():
    a = rs([(i,) for i in range(100)])
    b = rs([(i + 1000,) for i in range(100)])
    c = compare(a, b)
    assert c.only_in_source == 100
    assert len(c.paired) == 20


@pytest.mark.parametrize(
    "a,b,expected",
    [
        (("num", "1"), ("num", "1"), (0.0, 0.0)),
        (("num", "1"), ("num", "2"), (1.0, 0.5)),
        (("float", 2.0), ("num", "1"), (1.0, 0.5)),
        (("num", "0"), ("num", "0"), (0.0, 0.0)),
        (("str", "1"), ("num", "1"), None),
        (("null", None), ("num", "1"), None),
        (("float", math.inf), ("float", math.inf), (0.0, 0.0)),
        (("float", math.inf), ("float", 1.0), (math.inf, math.inf)),
        (("float", math.nan), ("float", math.nan), (0.0, 0.0)),
        (("float", math.nan), ("float", 1.0), (math.inf, math.inf)),
        (("num", "0"), ("float", 1e-300), (1e-300, 1.0)),
    ],
)
def test_numeric_gap(a, b, expected):
    assert numeric_gap(a, b) == expected


@settings(max_examples=60)
@given(st.lists(st.tuples(st.integers(-50, 50), st.text(max_size=3)), max_size=25))
def test_compare_is_reflexive(rows):
    a = rs(rows, types=("int", "string"))
    assert compare(a, a, ordered=True).identical


@settings(max_examples=60)
@given(st.lists(st.integers(-50, 50), max_size=25), st.lists(st.integers(-50, 50), max_size=25))
def test_compare_is_symmetric_in_status(xs, ys):
    a, b = rs([(x,) for x in xs]), rs([(y,) for y in ys])
    assert compare(a, b).status == compare(b, a).status
    assert compare(a, b).only_in_source == compare(b, a).only_in_target


@settings(max_examples=60)
@given(st.lists(st.integers(-50, 50), min_size=1, max_size=25), st.randoms())
def test_permutation_is_identical_unordered(xs, rnd):
    ys = list(xs)
    rnd.shuffle(ys)
    assert compare(rs([(x,) for x in xs]), rs([(y,) for y in ys])).identical


@settings(max_examples=60)
@given(st.lists(st.integers(-50, 50), min_size=1, max_size=25), st.integers(0, 24))
def test_any_single_change_is_caught(xs, idx):
    idx = idx % len(xs)
    ys = list(xs)
    ys[idx] = ys[idx] + 1000
    c = compare(rs([(x,) for x in xs]), rs([(y,) for y in ys]))
    assert c.status == "different"
    assert c.only_in_source == 1 and c.only_in_target == 1
