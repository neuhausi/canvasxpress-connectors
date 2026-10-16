"""SQL data source — any database SQLAlchemy can reach.

    SqlSource("postgresql+psycopg://user:pw@host/db",
              "SELECT sample, geneA, geneB, category FROM expression").read()

The SQL is provided by the data owner (server-side config), never by the browser.
A read-only guard rejects anything that isn't a single ``SELECT`` as defense in depth;
pair it with a least-privilege read-only database user in production.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Sequence, Tuple

_SELECT_ONLY = re.compile(r"^\s*select\b", re.IGNORECASE)


class ReadOnlyViolation(ValueError):
    """Raised when a statement is not a single SELECT."""


def assert_read_only(sql: str) -> None:
    stripped = sql.strip().rstrip(";")
    if ";" in stripped or not _SELECT_ONLY.match(stripped):
        raise ReadOnlyViolation("Only a single SELECT statement is allowed")


# Fallback matcher for a `:name` bind parameter — a colon NOT preceded by another
# colon (so Postgres `::type` casts are skipped) followed by an identifier.
_BIND_RE = re.compile(r"(?<!:):([A-Za-z_]\w*)")


def bind_param_names(sql: str) -> List[str]:
    """The names of the ``:name`` bind parameters a SELECT declares.

    Used to forward *only* the request parameters a query actually asks for
    (as bound parameters — never string-interpolated), so an unrelated or
    injected query key is never passed to the database. Prefers SQLAlchemy's own
    parser (which correctly ignores ``::casts`` and escaped colons); falls back
    to a regex when SQLAlchemy is unavailable.

    :param sql: The SELECT statement.
    :returns: Distinct bind-parameter names, in first-seen order.
    """
    try:
        from sqlalchemy import text

        names = list(text(sql)._bindparams.keys())
    except Exception:
        names = _BIND_RE.findall(sql)
    seen = set()
    ordered: List[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


class SqlSource:
    """A read-only SELECT against a SQLAlchemy connection URL."""

    def __init__(self, conn_url: str, sql: str, params: Optional[dict] = None):
        assert_read_only(sql)
        self.conn_url = conn_url
        self.sql = sql
        self.params = params or {}
        # Set by read(): True when a row_cap was hit and rows were dropped.
        self.truncated = False

    def read(self, row_cap: Optional[int] = None) -> Tuple[Sequence[str], Sequence[Sequence[Any]]]:
        """Run the SELECT and return ``(header, rows)``.

        :param row_cap: When given, at most ``row_cap`` rows are kept and
            ``self.truncated`` is set if the result had more. Without it every
            row is read (``fetchall``) — a multi-million-row table (e.g. a
            Databricks ``SELECT *`` saved source) would otherwise load entirely
            into memory and can exhaust the process, so callers serving the
            browser pass the same cap pushdown/join use (``CX_MAX_ROWS``).
        """
        # Imported lazily so the core package doesn't require SQLAlchemy.
        from sqlalchemy import create_engine, text

        self.truncated = False
        engine = create_engine(self.conn_url, future=True)
        try:
            with engine.connect() as conn:
                result = conn.execute(text(self.sql), self.params)
                header: List[str] = list(result.keys())
                if row_cap is None:
                    rows = [list(r) for r in result.fetchall()]
                else:
                    # Ask for one past the cap so truncation is detectable, then drop it.
                    fetched = result.fetchmany(row_cap + 1)
                    self.truncated = len(fetched) > row_cap
                    rows = [list(r) for r in fetched[:row_cap]]
        finally:
            engine.dispose()
        return header, rows
