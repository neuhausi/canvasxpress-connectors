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


class SqlSource:
    """A read-only SELECT against a SQLAlchemy connection URL."""

    def __init__(self, conn_url: str, sql: str, params: Optional[dict] = None):
        assert_read_only(sql)
        self.conn_url = conn_url
        self.sql = sql
        self.params = params or {}

    def read(self) -> Tuple[Sequence[str], Sequence[Sequence[Any]]]:
        # Imported lazily so the core package doesn't require SQLAlchemy.
        from sqlalchemy import create_engine, text

        engine = create_engine(self.conn_url, future=True)
        try:
            with engine.connect() as conn:
                result = conn.execute(text(self.sql), self.params)
                header: List[str] = list(result.keys())
                rows = [list(r) for r in result.fetchall()]
        finally:
            engine.dispose()
        return header, rows
