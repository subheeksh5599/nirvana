import datetime as dt
import math
from decimal import Decimal

import pytest
from hypothesis import given, strategies as st

from nirvana.canon import (
    NULL,
    CanonError,
    encode,
    fingerprint,
    row_key,
    row_tokens,
    token,
)
from nirvana.model import Column, ResultSet, decimal_scale, type_family


@pytest.mark.parametrize(
    "name,family",
    [
        ("int", "integer"), ("INT", "integer"), ("bigint", "integer"), ("BIGINT", "integer"),
        ("smallint", "integer"), ("tinyint", "integer"), ("long", "integer"), ("integer", "integer"),
        ("hugeint", "integer"), ("double", "float"), ("DOUBLE", "float"), ("float", "float"),
        ("real", "float"), ("double precision", "float"), ("decimal(7,2)", "decimal"),
        ("DECIMAL(38, 10)", "decimal"), ("numeric", "decimal"), ("number(10,0)", "decimal"),
        ("string", "string"), ("varchar(50)", "string"), ("char(1)", "string"), ("text", "string"),
        ("date", "date"), ("timestamp", "timestamp"), ("timestamp_ntz", "timestamp"),
        ("timestamp with time zone", "timestamp"), ("boolean", "boolean"), ("bool", "boolean"),
        ("array<int>", "other"), ("map<string,int>", "other"), ("binary", "other"), ("  int ", "integer"),
    ],
)
def test_type_family(name, family):
    assert type_family(name) == family


@pytest.mark.parametrize(
    "name,scale",
    [("decimal(7,2)", 2), ("decimal(38, 10)", 10), ("decimal(7)", 0), ("numeric(5,0)", 0), ("double", None), ("decimal", None), ("decimal(7,x)", None)],
)
def test_decimal_scale(name, scale):
    assert decimal_scale(name) == scale


def test_resultset_rejects_ragged_rows():
    with pytest.raises(ValueError, match="row 1 has 1 values"):
        ResultSet.build("e", "select 1", [Column("a", "int"), Column("b", "int")], [(1, 2), (3,)])


def test_resultset_build_freezes_rows():
    rs = ResultSet.build("e", "q", [Column("a", "int")], [[1], [2]])
    assert rs.rows == ((1,), (2,))
    assert rs.row_count == 2
    assert rs.column_names == ["a"]


@pytest.mark.parametrize(
    "value,expected",
    [
        (Decimal("1.50"), "1.5"), (Decimal("1.5"), "1.5"), ("1.500", "1.5"), (1, "1"), ("1", "1"),
        (Decimal("100"), "100"), (Decimal("1E+2"), "100"), (Decimal("0.000"), "0"), (Decimal("-0"), "0"),
        (Decimal("-12.340"), "-12.34"), (Decimal("0.001"), "0.001"), (" 42 ", "42"), (10**20, "100000000000000000000"),
        (Decimal("123456789.123456789"), "123456789.123456789"), (2.5, "2.5"), (Decimal("-0.50"), "-0.5"),
    ],
)
def test_exact_numbers_normalize(value, expected):
    assert token(value, "decimal") == ("num", expected)
    assert token(value, "integer") == ("num", expected)


def test_integer_and_decimal_of_same_value_compare_equal():
    assert token(7, "integer") == token(Decimal("7.00"), "decimal")


@pytest.mark.parametrize("bad", ["abc", "", "1.2.3", True, object()])
def test_exact_number_rejects_garbage(bad):
    with pytest.raises(CanonError):
        token(bad, "decimal")


@pytest.mark.parametrize("value,expected", [(1.0, 1.0), ("2.25", 2.25), (Decimal("0.1"), 0.1), (3, 3.0), ("inf", math.inf), ("-Infinity", -math.inf)])
def test_float_tokens(value, expected):
    kind, v = token(value, "float")
    assert kind == "float" and v == expected


def test_float_nan_token_and_encoding():
    kind, v = token("NaN", "float")
    assert kind == "float" and math.isnan(v)
    assert encode(("float", v)) == "float:nan"


def test_nonfinite_float_in_decimal_column_stays_float():
    assert token(math.inf, "decimal") == ("float", math.inf)


@pytest.mark.parametrize(
    "value,expected",
    [
        (dt.date(2001, 1, 2), "2001-01-02"), ("2001-01-02", "2001-01-02"), (dt.datetime(2001, 1, 2, 3, 4), "2001-01-02"),
        ("2001-01-02T00:00:00", "2001-01-02"), ("2001-01-02 10:00:00", "2001-01-02"),
    ],
)
def test_dates(value, expected):
    assert token(value, "date") == ("date", expected)


@pytest.mark.parametrize("bad", ["2001-13-02", "yesterday", 20010102])
def test_bad_dates(bad):
    with pytest.raises(CanonError):
        token(bad, "date")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2001-01-02 03:04:05", "2001-01-02T03:04:05.000000"),
        ("2001-01-02T03:04:05Z", "2001-01-02T03:04:05.000000"),
        ("2001-01-02T05:04:05+02:00", "2001-01-02T03:04:05.000000"),
        (dt.datetime(2001, 1, 2, 3, 4, 5, 6), "2001-01-02T03:04:05.000006"),
        (dt.date(2001, 1, 2), "2001-01-02T00:00:00.000000"),
        (dt.datetime(2001, 1, 2, 3, tzinfo=dt.timezone(dt.timedelta(hours=-5))), "2001-01-02T08:00:00.000000"),
    ],
)
def test_timestamps(value, expected):
    assert token(value, "timestamp") == ("ts", expected)


def test_bad_timestamp():
    with pytest.raises(CanonError):
        token("not a time", "timestamp")
    with pytest.raises(CanonError):
        token(12, "timestamp")


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("true", True), ("FALSE", False), (1, True), (0, False), ("t", True), ("0", False)])
def test_booleans(value, expected):
    assert token(value, "boolean") == ("bool", expected)


@pytest.mark.parametrize("bad", ["yes", 2, "maybe"])
def test_bad_booleans(bad):
    with pytest.raises(CanonError):
        token(bad, "boolean")


@pytest.mark.parametrize("value,expected", [("abc", "abc"), ("abc ", "abc "), ("ABC", "ABC"), (12, "12"), ("", "")])
def test_strings_are_kept_byte_for_byte(value, expected):
    assert token(value, "string") == ("str", expected)


def test_trailing_space_is_a_difference():
    assert token("x", "string") != token("x ", "string")


def test_case_is_a_difference():
    assert token("x", "string") != token("X", "string")


def test_other_family_is_stable_for_json_values():
    assert token({"b": 1, "a": 2}, "other") == token({"a": 2, "b": 1}, "other")
    assert token([1, 2], "other") == ("str", "[1, 2]")


@pytest.mark.parametrize("family", ["integer", "decimal", "float", "string", "date", "timestamp", "boolean", "other"])
def test_null_is_null_in_every_family(family):
    assert token(None, family) == NULL


def test_row_tokens_checks_width():
    with pytest.raises(CanonError):
        row_tokens([1, 2], [Column("a", "int")])


def test_row_tokens_uses_each_column_type():
    cols = [Column("a", "int"), Column("b", "string"), Column("c", "date")]
    assert row_tokens([1, 1, "2001-01-01"], cols) == (("num", "1"), ("str", "1"), ("date", "2001-01-01"))


def test_row_key_separates_fields():
    a = row_key([("str", "a"), ("str", "b")])
    b = row_key([("str", "a\x1fb")])
    assert a != b  # json encoding escapes the separator inside a value
    assert "\\u001f" in b


def test_fingerprint_multiset_ignores_order():
    r1 = [(("num", "1"),), (("num", "2"),)]
    r2 = list(reversed(r1))
    assert fingerprint(r1, ordered=False) == fingerprint(r2, ordered=False)
    assert fingerprint(r1, ordered=True) != fingerprint(r2, ordered=True)


def test_fingerprint_counts_duplicates():
    one = [(("num", "1"),)]
    two = one * 2
    assert fingerprint(one, ordered=False) != fingerprint(two, ordered=False)


def test_fingerprint_modes_differ_even_for_same_rows():
    rows = [(("num", "1"),)]
    assert fingerprint(rows, ordered=False) != fingerprint(rows, ordered=True)


@given(st.decimals(min_value=-(10**15), max_value=10**15, allow_nan=False, allow_infinity=False, places=4))
def test_decimal_scale_never_changes_identity(d):
    padded = d.quantize(Decimal("0.0001"))
    assert token(d, "decimal") == token(padded, "decimal")


@given(st.integers(min_value=-(10**30), max_value=10**30))
def test_integers_roundtrip_through_text(i):
    assert token(i, "integer") == token(str(i), "integer") == ("num", str(i))


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_float_tokens_are_exact(f):
    assert token(f, "float") == ("float", f)
    assert token(repr(f), "float") == ("float", f)


@given(st.lists(st.integers(), max_size=30))
def test_unordered_fingerprint_is_permutation_invariant(xs):
    rows = [(("num", str(x)),) for x in xs]
    assert fingerprint(rows, ordered=False) == fingerprint(sorted(rows), ordered=False)


@given(st.dates())
def test_dates_roundtrip(d):
    assert token(d, "date") == token(d.isoformat(), "date")
