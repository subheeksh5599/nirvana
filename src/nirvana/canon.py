"""Turn engine values into comparable tokens.

Two engines rarely hand back the same Python objects for the same answer: one returns
``Decimal('1.50')``, the other the JSON string ``"1.5"``; one returns ``datetime.date``,
the other ``"2001-01-02"``. A token is ``(kind, value)`` where kind is one of

* ``null``  - SQL NULL
* ``num``   - an exact number (integer or decimal), value is the normalized decimal string
* ``float`` - an approximate number, value is a Python float
* ``str``   - text, compared byte for byte (no trimming, no case folding)
* ``date``  - ``YYYY-MM-DD``
* ``ts``    - ``YYYY-MM-DDTHH:MM:SS.ffffff`` in UTC when the engine gave an offset
* ``bool``  - True / False

Text is never trimmed or case-folded here: a trailing space that one engine keeps and the
other drops is a real difference and the diagnoser names it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from decimal import Context, Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

from .model import Column

Token = tuple[str, Any]
NULL: Token = ("null", None)
_WIDE = Context(prec=1000)


class CanonError(ValueError):
    pass


def _decimal_text(d: Decimal) -> str:
    if not d.is_finite():
        raise CanonError(f"non-finite decimal {d!r}")
    if d == 0:
        return "0"
    # The default context keeps 28 digits and would round a 30-digit integer; use a wide one.
    n = d.normalize(_WIDE)
    sign, digits, raw_exp = n.as_tuple()
    exp = int(raw_exp)  # finite, so the exponent is an int
    if exp >= 0:
        text = "".join(map(str, digits)) + "0" * exp
    else:
        s = "".join(map(str, digits))
        point = len(s) + exp
        if point > 0:
            text = s[:point] + "." + s[point:]
        else:
            text = "0." + "0" * (-point) + s
    return ("-" if sign else "") + text


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise CanonError("boolean is not a number")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonError(f"non-finite float {value!r}")
        return Decimal(repr(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation as e:
            raise CanonError(f"not a number: {value!r}") from e
    raise CanonError(f"cannot read {type(value).__name__} as a number")


def _to_float(value: Any) -> float:
    if isinstance(value, bool):
        raise CanonError("boolean is not a number")
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, str):
        v = value.strip().lower()
        special = {"nan": math.nan, "infinity": math.inf, "inf": math.inf, "-infinity": -math.inf, "-inf": -math.inf}
        if v in special:
            return special[v]
        try:
            return float(v)
        except ValueError as e:
            raise CanonError(f"not a number: {value!r}") from e
    raise CanonError(f"cannot read {type(value).__name__} as a float")


def _to_date(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()[:10]
        try:
            return dt.date.fromisoformat(text).isoformat()
        except ValueError as e:
            raise CanonError(f"not a date: {value!r}") from e
    raise CanonError(f"cannot read {type(value).__name__} as a date")


def _to_ts(value: Any) -> str:
    if isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, dt.date):
        moment = dt.datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        text = value.strip().replace(" ", "T", 1)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = dt.datetime.fromisoformat(text)
        except ValueError as e:
            raise CanonError(f"not a timestamp: {value!r}") from e
    else:
        raise CanonError(f"cannot read {type(value).__name__} as a timestamp")
    if moment.tzinfo is not None:
        moment = moment.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return moment.isoformat(timespec="microseconds")


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, Decimal)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "t", "f"):
        return value.strip().lower() in ("true", "1", "t")
    raise CanonError(f"not a boolean: {value!r}")


def token(value: Any, family: str) -> Token:
    """Canonical token for one value read from a column of the given type family."""
    if value is None:
        return NULL
    if family in ("integer", "decimal"):
        if isinstance(value, float) and not math.isfinite(value):
            return ("float", value)
        return ("num", _decimal_text(_to_decimal(value)))
    if family == "float":
        return ("float", _to_float(value))
    if family == "date":
        return ("date", _to_date(value))
    if family == "timestamp":
        return ("ts", _to_ts(value))
    if family == "boolean":
        return ("bool", _to_bool(value))
    if family == "string":
        return ("str", value if isinstance(value, str) else str(value))
    # Unknown type: keep a stable text form so equal values still compare equal.
    if isinstance(value, (dict, list)):
        return ("str", json.dumps(value, sort_keys=True, default=str))
    return ("str", str(value))


def row_tokens(row: Sequence[Any], columns: Sequence[Column]) -> tuple[Token, ...]:
    if len(row) != len(columns):
        raise CanonError(f"row has {len(row)} values for {len(columns)} columns")
    return tuple(token(v, c.family) for v, c in zip(row, columns))


def encode(tok: Token) -> str:
    kind, value = tok
    if kind == "float":
        if math.isnan(value):
            return "float:nan"
        return "float:" + repr(value)
    if kind == "null":
        return "null"
    return f"{kind}:{json.dumps(value)}"


def row_key(tokens: Iterable[Token]) -> str:
    return "\x1f".join(encode(t) for t in tokens)


def fingerprint(rows: Iterable[tuple[Token, ...]], *, ordered: bool) -> str:
    """SHA-256 over the encoded rows. Unordered fingerprints sort the rows first, so
    two engines that return the same multiset in different orders agree."""
    keys = [row_key(r) for r in rows]
    if not ordered:
        keys.sort()
    h = hashlib.sha256()
    h.update(b"ordered\n" if ordered else b"multiset\n")
    for k in keys:
        h.update(k.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()
