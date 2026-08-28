"""Shared ETL helpers: downloads, gunzip, and building a SQLite DB from
tab-separated files.

These reproduce the ``.mode csv`` / ``.separator "\\t"`` / ``.import file table``
dance the original Perl ``create_database`` subs used, but in Python with the
stdlib ``sqlite3`` module — so a row is split on TABs and inserted with bound
parameters (no shelling out to the ``sqlite3`` CLI). Packed BLOB columns are just
the JSON-array *string* the reshape step wrote; SQLite stores it verbatim.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import sqlite3
import subprocess
import urllib.request
from typing import Iterable, List, Optional, Sequence, Tuple

_CREATE_TABLE_RE = re.compile(r"CREATE\s+TABLE\s+\[?(\w+)\]?\s*\((.*?)\)\s*;", re.IGNORECASE | re.DOTALL)


def download(url: str, dest: str, headers: Optional[dict] = None) -> str:
    """Download ``url`` to ``dest`` (streamed). Returns ``dest``.

    :param url: Source URL.
    :param dest: Destination path.
    :param headers: Optional request headers.
    :returns: ``dest``.
    """
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "cxdb-etl"})
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)
    return dest


def gunzip(src: str, dest: Optional[str] = None) -> str:
    """Decompress a gzip file. Defaults ``dest`` to ``src`` without ``.gz``."""
    if dest is None:
        dest = src[:-3] if src.endswith(".gz") else src + ".out"
    with gzip.open(src, "rb") as fin, open(dest, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    return dest


def run(cmd: Sequence[str]) -> None:
    """Run a subprocess, raising on failure (for unzip and the like)."""
    subprocess.run(list(cmd), check=True)


def table_columns(schema_sql: str) -> dict:
    """Map each ``CREATE TABLE`` name to its ordered column names, parsed from a
    schema string. Used to size the INSERT for each imported TSV.

    :param schema_sql: One or more ``CREATE TABLE`` statements.
    :returns: ``{table_name: [col, ...]}``.
    """
    out = {}
    for name, body in _CREATE_TABLE_RE.findall(schema_sql):
        cols = []
        for line in body.split(","):
            line = line.strip()
            m = re.match(r"\[?(\w+)\]?", line)
            if m and line and not line.upper().startswith(("PRIMARY", "FOREIGN", "UNIQUE", "CHECK")):
                cols.append(m.group(1))
        out[name] = cols
    return out


def build_sqlite(db_path: str, schema_sql: str,
                 imports: Iterable[Tuple[str, str]], sep: str = "\t") -> None:
    """Create ``db_path`` from ``schema_sql`` and import tab-separated files.

    :param db_path: Output SQLite path (overwritten if it exists).
    :param schema_sql: ``CREATE TABLE`` / ``CREATE INDEX`` statements.
    :param imports: ``(file_path, table_name)`` pairs to import, in order.
    :param sep: Field separator in the import files (default TAB).
    """
    if os.path.exists(db_path):
        os.remove(db_path)
    cols_by_table = table_columns(schema_sql)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        for file_path, table in imports:
            ncols = len(cols_by_table.get(table, []))
            placeholders = ",".join(["?"] * ncols) if ncols else None
            with open(file_path, encoding="utf-8") as handle:
                rows = []
                for line in handle:
                    line = line.rstrip("\n")
                    if line == "":
                        continue
                    fields = line.split(sep)
                    if ncols:
                        # Pad/truncate to the column count (mirrors sqlite .import).
                        fields = (fields + [None] * ncols)[:ncols]
                    rows.append(fields)
                    if len(rows) >= 5000:
                        _insert(conn, table, placeholders, rows)
                        rows = []
                if rows:
                    _insert(conn, table, placeholders, rows)
        conn.commit()
    finally:
        conn.close()


def _insert(conn, table: str, placeholders: Optional[str], rows: List[list]) -> None:
    if placeholders:
        conn.executemany("INSERT INTO [%s] VALUES (%s)" % (table, placeholders), rows)
    else:
        conn.executemany("INSERT INTO [%s] VALUES (?)" % table, [(r[0],) for r in rows])
