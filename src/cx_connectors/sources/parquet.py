"""Parquet data source — one or more Parquet files, local or remote, via DuckDB.

    ParquetSource("orders.parquet").read()
    ParquetSource("s3://bucket/orders/*.parquet",
                  s3={"region": "us-east-1",
                      "access_key_id": "…", "secret_access_key": "…"}).read()

``ParquetSource`` is a thin convenience over :class:`SqlSource`: it builds a DuckDB
connection URL plus a ``SELECT … FROM read_parquet('<path|url>')`` SELECT from just a
file path or URL, so it **is** a ``SqlSource``. Every downstream reader — ``to_cx``,
``/api/data``, ``run_pushdown``, ``run_join`` and ``/api/columns`` — works on it
unchanged, because the pushdown/join paths rebuild the engine from the connection URL
alone (a bare ``sa.create_engine(conn_url)``, no ``connect_args``).

The design decision that makes that work (plan P0, design A): **all** DuckDB / httpfs /
S3 configuration rides *inside* the connection URL as query parameters, so it survives
that bare engine rebuild. DuckDB 1.x/2.x auto-installs and auto-loads ``httpfs`` on the
first ``https://``/``s3://`` read, so no explicit ``INSTALL`` is needed.

The Parquet location is a server-side literal supplied by the data owner, never a browser
value — the same trust model as ``SqlSource``'s owner-written SQL. Browser-driven
filtering/aggregation stays on the pushdown path, unchanged.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from .sql import SqlSource

# DuckDB settings carried by the ``s3`` dict, mapped to their DuckDB config names.
# Values ride the connection URL as query params; duckdb-engine applies them as
# DuckDB ``SET``/config on connect.
_S3_KEYS = {
    "region": "s3_region",
    "access_key_id": "s3_access_key_id",
    "secret_access_key": "s3_secret_access_key",
    "endpoint": "s3_endpoint",
    "session_token": "s3_session_token",
    "url_style": "s3_url_style",
    "use_ssl": "s3_use_ssl",
}


def _quote_literal(text: str) -> str:
    """Quote a Parquet path/URL into a DuckDB single-quoted string literal.

    Rejects an embedded single quote outright (raises :class:`ValueError`) as
    defense in depth: the location is owner-supplied config, so a quote has no
    legitimate use and would otherwise break out of the string literal.

    :param text: The Parquet file path or URL.
    :returns: The path wrapped in single quotes, ready to drop into SQL.
    :raises ValueError: The path contains a single quote.
    :private:
    """
    if "'" in text:
        raise ValueError("A Parquet path/URL may not contain a single quote")
    return "'" + text + "'"


def build_read_parquet_sql(source, columns=None, where=None):
    """Build the ``SELECT … FROM read_parquet(<path>)`` the source reads.

    :param source: A Parquet path/URL, or a list of them (DuckDB globs/unions them).
    :param columns: Optional list of column identifiers to project; absent → ``*``.
    :param where: Optional owner-supplied static ``WHERE`` clause (without the
        ``WHERE`` keyword). Browser-driven filtering uses the pushdown path, not this.
    :returns: A single read-only SELECT statement.
    :raises ValueError: A path contains a single quote, or a column name is unsafe.
    :public:
    """
    if isinstance(source, (list, tuple)):
        literal = "[" + ", ".join(_quote_literal(str(s)) for s in source) + "]"
    else:
        literal = _quote_literal(str(source))

    if columns:
        safe = []
        for col in columns:
            name = str(col)
            if "'" in name or '"' in name or ";" in name:
                raise ValueError("Unsafe column name: " + name)
            safe.append('"' + name + '"')
        projection = ", ".join(safe)
    else:
        projection = "*"

    sql = "SELECT " + projection + " FROM read_parquet(" + literal + ")"
    if where:
        clause = str(where).strip().rstrip(";")
        if ";" in clause:
            raise ValueError("A WHERE clause may not contain a semicolon")
        sql += " WHERE " + clause
    return sql


def build_duckdb_url(s3=None, db=None):
    """Build the DuckDB connection URL carrying any DuckDB/S3 configuration.

    Design A (plan P0): the config rides the URL query string so a bare
    ``sa.create_engine(conn_url)`` (as ``run_pushdown``/``run_join`` use) inherits it.

    :param s3: Optional dict of S3 settings (``region``, ``access_key_id``,
        ``secret_access_key``, ``endpoint``, ``session_token``, ``url_style``,
        ``use_ssl``); omit for local or public reads.
    :param db: Optional DuckDB database path; defaults to an in-memory database.
    :returns: A ``duckdb:///…`` SQLAlchemy URL, with S3 settings as query params.
    :public:
    """
    base = "duckdb:///" + (db if db else ":memory:")
    if not s3:
        return base
    pairs = []
    for key, value in s3.items():
        if value is None:
            continue
        duck_key = _S3_KEYS.get(key, key)
        # Keep "*" literal so a "********" mask survives round-trip through the edit form
        # and connmask.unmask_conn_url can still match it (same convention as connmask).
        pairs.append(duck_key + "=" + quote_plus(str(value), safe="*"))
    if not pairs:
        return base
    return base + "?" + "&".join(pairs)


class ParquetSource(SqlSource):
    """A read-only CanvasXpress source over one or more Parquet files, via DuckDB.

    Builds a DuckDB connection URL + a ``SELECT … FROM read_parquet('<path|url>')``
    SELECT, so it IS a :class:`SqlSource`: ``read``, ``to_cx``, pushdown, join and
    ``/api/columns`` all work on it. Supports a local filesystem path and remote
    ``s3://`` / ``https://`` objects (DuckDB httpfs; S3 credentials ride in the
    connection URL — plan P0 design A).

    :param source: A Parquet file path or URL (or a list of them).
    :param columns: Optional list of columns to project; absent → all columns.
    :param s3: Optional dict of S3 settings (see :func:`build_duckdb_url`).
    :param where: Optional owner-supplied static WHERE clause.
    :param db: Optional DuckDB database path; defaults to in-memory.
    :raises ValueError: An unsafe path/column, or the built SQL is not read-only.
    :public:
    """

    def __init__(self, source, columns=None, *, s3=None, where=None, db=None):
        conn_url = build_duckdb_url(s3=s3, db=db)
        sql = build_read_parquet_sql(source, columns, where)
        super().__init__(conn_url, sql)
        self.parquet = source
