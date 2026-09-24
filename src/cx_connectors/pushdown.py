"""Pushdown queries: aggregate, filter and limit *in the database*.

A SQL source's owner writes one read-only ``SELECT``. A dashboard can then ask
for a smaller answer from it without writing SQL, by sending a declarative
*pushdown query*::

    {"groupBy": ["region", "month"],
     "measures": [{"fn": "sum", "column": "revenue"}, {"fn": "count"}],
     "where": [{"column": "status", "op": "in", "value": ["won", "open"]},
               {"column": "amount", "op": ">=", "value": 1000}],
     "orderBy": [{"column": "sum_revenue", "desc": true}],
     "limit": 500}

It is compiled with SQLAlchemy Core around the owner's query, used as a
subquery. So the database scans, filters, groups and sorts, and only the result
travels::

    SELECT region, month, sum(revenue) AS sum_revenue, count(*) AS count
      FROM (<owner's SELECT>) AS cxq
     WHERE status IN (:p1, :p2) AND amount >= :p3
     GROUP BY region, month ORDER BY sum_revenue DESC LIMIT 501

Safety: nothing from the request becomes SQL text.

- Column names must be columns of the owner's query (read from the database)
  and are quoted by the dialect.
- Functions and operators come from fixed lists.
- Values are bound parameters.
- The owner's own ``:name`` parameters are forwarded as before.

Without ``groupBy`` or ``measures`` a query returns rows (optionally only the
``columns`` listed), filtered and capped. With ``measures`` and no ``groupBy``
it returns one row of totals, labelled ``all``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

FUNCTIONS = ("count", "count_distinct", "sum", "avg", "min", "max")
OPERATORS = ("=", "!=", "<", "<=", ">", ">=", "in", "not_in", "between", "is_null", "not_null")
MAX_LIMIT = 1_000_000


class PushdownError(ValueError):
    """A pushdown query that cannot be run (bad shape, unknown column, …)."""


def parse_query(raw: Any) -> Dict[str, Any]:
    """Validate a pushdown query (a dict or its JSON text) and return it normalized."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise PushdownError("The pushdown query is not valid JSON")
    if not isinstance(raw, dict):
        raise PushdownError("A pushdown query is an object")
    unknown = set(raw) - {"columns", "groupBy", "measures", "where", "orderBy", "limit"}
    if unknown:
        raise PushdownError("Unknown pushdown keys: " + ", ".join(sorted(unknown)))
    out: Dict[str, Any] = {
        "columns": _names(raw.get("columns"), "columns"),
        "groupBy": _names(raw.get("groupBy"), "groupBy"),
        "measures": [],
        "where": [],
        "orderBy": [],
        "limit": None,
    }
    for m in raw.get("measures") or []:
        if not isinstance(m, dict):
            raise PushdownError("Each measure is {fn, column?, as?}")
        fn = m.get("fn")
        if fn == "mean":
            fn = "avg"
        if fn not in FUNCTIONS:
            raise PushdownError("fn must be one of: " + ", ".join(FUNCTIONS))
        col = m.get("column")
        if fn != "count" and not isinstance(col, str):
            raise PushdownError("%s needs a column" % fn)
        alias = m.get("as") or (fn if fn == "count" and not col else "%s_%s" % (fn, col))
        if not isinstance(alias, str) or not alias:
            raise PushdownError("A measure's 'as' must be a name")
        out["measures"].append({"fn": fn, "column": col, "as": alias})
    for w in raw.get("where") or []:
        if not isinstance(w, dict) or not isinstance(w.get("column"), str):
            raise PushdownError("Each filter is {column, op, value}")
        op = w.get("op") or "="
        if op not in OPERATORS:
            raise PushdownError("op must be one of: " + " ".join(OPERATORS))
        value = w.get("value")
        if op in ("in", "not_in"):
            if not isinstance(value, list):
                raise PushdownError("'%s' takes a list of values" % op)
            value = [_scalar(v) for v in value]
        elif op == "between":
            if not (isinstance(value, list) and len(value) == 2):
                raise PushdownError("'between' takes [low, high]")
            value = [_scalar(v) for v in value]
        elif op not in ("is_null", "not_null"):
            value = _scalar(value)
        out["where"].append({"column": w["column"], "op": op, "value": value})
    for o in raw.get("orderBy") or []:
        if isinstance(o, str):
            o = {"column": o}
        if not isinstance(o, dict) or not isinstance(o.get("column"), str):
            raise PushdownError("Each orderBy entry is a name or {column, desc}")
        out["orderBy"].append({"column": o["column"], "desc": bool(o.get("desc"))})
    limit = raw.get("limit")
    if limit is not None:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 0 < limit <= MAX_LIMIT:
            raise PushdownError("limit must be a whole number from 1 to %d" % MAX_LIMIT)
        out["limit"] = limit
    if out["columns"] and (out["groupBy"] or out["measures"]):
        raise PushdownError("Use 'columns' for rows, or 'groupBy'/'measures' for aggregates")
    return out


def _names(value: Any, what: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise PushdownError("'%s' is a list of column names" % what)
    return list(value)


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise PushdownError("Filter values are strings, numbers, booleans or null")


def build_statement(sql: Any, columns: Sequence[str], query: Dict[str, Any], row_cap: int):
    """The SQLAlchemy ``Select`` for ``query`` over the owner's ``sql``.

    :param sql: The owner's SELECT text, or an already built subquery (a join).
    :param columns: The source's column names (from the database).
    :param row_cap: Rows to return at most; one more is asked for so the
        caller can tell the result was cut.
    """
    import sqlalchemy as sa

    if isinstance(sql, str):
        sub = sa.text(sql).columns(*[sa.column(c) for c in columns]).subquery("cxq")
    else:
        sub = sql
    cols = {c: sub.c[c] for c in columns}

    def col(name: str):
        if name not in cols:
            raise PushdownError("No column named %r in this source (it has: %s)"
                                % (name, ", ".join(columns)))
        return cols[name]

    outputs: Dict[str, Any] = {}
    selected: List[Any] = []
    grouped = bool(query["groupBy"] or query["measures"])
    if grouped:
        for name in query["groupBy"]:
            selected.append(col(name))
            outputs[name] = col(name)
        if not query["groupBy"]:
            label = sa.literal("all").label("group")
            selected.append(label)
            outputs["group"] = label
        for m in query["measures"]:
            if m["as"] in outputs:
                raise PushdownError("Two outputs are named %r" % m["as"])
            fn = m["fn"]
            if fn == "count":
                expr = sa.func.count(col(m["column"])) if m["column"] else sa.func.count()
            elif fn == "count_distinct":
                expr = sa.func.count(sa.distinct(col(m["column"])))
            else:
                expr = getattr(sa.func, fn)(col(m["column"]))
            labelled = expr.label(m["as"])
            selected.append(labelled)
            outputs[m["as"]] = labelled
    else:
        for name in (query["columns"] or list(columns)):
            selected.append(col(name))
            outputs[name] = col(name)
    stmt = sa.select(*selected).select_from(sub)
    clauses = []
    for w in query["where"]:
        c, op, v = col(w["column"]), w["op"], w["value"]
        if op == "=":
            clauses.append(c.is_(None) if v is None else c == v)
        elif op == "!=":
            clauses.append(c.isnot(None) if v is None else c != v)
        elif op == "<":
            clauses.append(c < v)
        elif op == "<=":
            clauses.append(c <= v)
        elif op == ">":
            clauses.append(c > v)
        elif op == ">=":
            clauses.append(c >= v)
        elif op == "in":
            clauses.append(c.in_(v) if v else sa.false())
        elif op == "not_in":
            if v:
                clauses.append(c.not_in(v))
        elif op == "between":
            clauses.append(c.between(v[0], v[1]))
        elif op == "is_null":
            clauses.append(c.is_(None))
        else:
            clauses.append(c.isnot(None))
    if clauses:
        stmt = stmt.where(sa.and_(*clauses))
    if query["groupBy"]:
        stmt = stmt.group_by(*[col(g) for g in query["groupBy"]])
    for o in query["orderBy"]:
        if o["column"] not in outputs:
            raise PushdownError("orderBy %r is not an output column" % o["column"])
        # A labelled measure renders as its alias in ORDER BY.
        target = outputs[o["column"]]
        stmt = stmt.order_by(target.desc() if o["desc"] else target.asc())
    limit = min(query["limit"] or row_cap, row_cap)
    return stmt.limit(limit + 1), limit


def source_columns(conn, sql: str, params: Optional[dict] = None) -> List[str]:
    """The column names of the owner's query, without reading its rows."""
    import sqlalchemy as sa

    sub = sa.text(sql).columns().subquery("cxq")
    probe = sa.select(sa.literal_column("*")).select_from(sub).where(sa.false())
    return list(conn.execute(probe, params or {}).keys())


def run_pushdown(conn_url: str, sql: str, params: Optional[dict], query: Any,
                 row_cap: int = 100_000) -> Tuple[List[str], List[List[Any]], bool]:
    """Run a pushdown query; returns ``(header, rows, truncated)``."""
    import sqlalchemy as sa

    from .sources.sql import assert_read_only

    assert_read_only(sql)
    query = parse_query(query)           # idempotent: normalized queries pass through
    engine = sa.create_engine(conn_url, future=True)
    try:
        with engine.connect() as conn:
            columns = source_columns(conn, sql, params)
            stmt, limit = build_statement(sql, columns, query, row_cap)
            result = conn.execute(stmt, params or {})
            header = list(result.keys())
            rows = [list(r) for r in result.fetchall()]
    finally:
        engine.dispose()
    truncated = len(rows) > limit
    return header, rows[:limit], truncated


JOIN_TYPES = ("inner", "left", "right", "outer")


def run_join(left: Tuple[str, str, Optional[dict]], right: Tuple[str, str, Optional[dict]],
             on: Any, how: str = "inner", right_name: str = "right", query: Any = None,
             row_cap: int = 100_000) -> Tuple[List[str], List[List[Any]], bool]:
    """Join two SQL sources **in the database** and optionally aggregate the result.

    Mirrors the dashboards' ``kind:"join"``: the output holds the left's columns
    then the right's non-key columns (a clashing right name gets
    ``"." + right_name``), and its first column is the left's (the row id). A
    key named ``smps`` means that side's row id, its first column. Both
    sources must use the same database connection.

    :param left: ``(conn_url, sql, params)`` of the left source (same for ``right``).
    :param on: A column name on both sides, ``{left, right}``, or a list of those.
    :param query: An optional pushdown query run over the joined rows.
    :returns: ``(header, rows, truncated)``.
    :raises PushdownError: Different databases, an unknown key or join type.
    """
    import sqlalchemy as sa

    from .sources.sql import assert_read_only

    if how not in JOIN_TYPES:
        raise PushdownError("how must be one of: " + ", ".join(JOIN_TYPES))
    if left[0] != right[0]:
        raise PushdownError("The two sources are in different databases; join them in the browser")
    assert_read_only(left[1])
    assert_read_only(right[1])
    pairs = _join_pairs(on)
    query = parse_query(query or {})
    params = dict(left[2] or {})
    params.update(right[2] or {})
    engine = sa.create_engine(left[0], future=True)
    try:
        with engine.connect() as conn:
            lcols = source_columns(conn, left[1], params)
            rcols = source_columns(conn, right[1], params)
            lsub = sa.text(left[1]).columns(*[sa.column(c) for c in lcols]).subquery("cxl")
            rsub = sa.text(right[1]).columns(*[sa.column(c) for c in rcols]).subquery("cxr")
            keys = []
            for lk, rk in pairs:
                lk = lcols[0] if lk == "smps" else lk
                rk = rcols[0] if rk == "smps" else rk
                if lk not in lcols:
                    raise PushdownError("The left source has no column %r" % lk)
                if rk not in rcols:
                    raise PushdownError("The right source has no column %r" % rk)
                keys.append((lk, rk))
            cond = sa.and_(*[lsub.c[lk] == rsub.c[rk] for lk, rk in keys])
            if how == "right":
                joined_from = rsub.outerjoin(lsub, cond)
            else:
                joined_from = lsub.join(rsub, cond, isouter=how == "left", full=how == "outer")
            right_keys = {rk for _, rk in keys}
            key_of_left = {lk: rk for lk, rk in keys}
            selected, names = [], []
            for c in lcols:
                expr = lsub.c[c]
                if c in key_of_left and how in ("right", "outer"):
                    # rows only on the right still carry their key
                    expr = sa.func.coalesce(lsub.c[c], rsub.c[key_of_left[c]])
                elif c == lcols[0] and how in ("right", "outer"):
                    expr = sa.func.coalesce(lsub.c[c], rsub.c[rcols[0]])
                selected.append(expr.label(c))
                names.append(c)
            for c in rcols:
                if c in right_keys:
                    continue
                out = c
                while out in names:
                    out = out + "." + right_name
                selected.append(rsub.c[c].label(out))
                names.append(out)
            joined = sa.select(*selected).select_from(joined_from).subquery("cxj")
            stmt, limit = build_statement(joined, names, query, row_cap)
            result = conn.execute(stmt, params)
            header = list(result.keys())
            rows = [list(r) for r in result.fetchall()]
    finally:
        engine.dispose()
    return header, rows[:limit], len(rows) > limit


def _join_pairs(on: Any) -> List[Tuple[str, str]]:
    if isinstance(on, str) and on[:1] in ('[', '{', '"'):
        try:
            on = json.loads(on)          # the dashboards send JSON; a bare name is fine too
        except ValueError:
            raise PushdownError("'on' is not valid JSON")
    if on is None or on == "":
        on = "smps"
    items = on if isinstance(on, list) else [on]
    pairs = []
    for item in items:
        if isinstance(item, str) and item:
            pairs.append((item, item))
        elif isinstance(item, dict) and isinstance(item.get("left"), str) \
                and isinstance(item.get("right"), str):
            pairs.append((item["left"], item["right"]))
        else:
            raise PushdownError("'on' is a column name, {left, right}, or a list of those")
    if not pairs:
        raise PushdownError("A join needs at least one key")
    return pairs
