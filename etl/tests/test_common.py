import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cxdb_etl import common  # noqa: E402


def test_table_columns_parses_schema():
    schema = ("CREATE TABLE [t] ([a] TEXT NOT NULL, [b] INTEGER, [c] BLOB);"
              "CREATE INDEX [i] ON [t] ([a]);")
    assert common.table_columns(schema) == {"t": ["a", "b", "c"]}


def test_build_sqlite_imports_tsv(tmp_path):
    schema = "CREATE TABLE [t] ([a] TEXT, [b] INTEGER);"
    data = tmp_path / "t.txt"
    data.write_text("x\t1\ny\t2\n")
    db = str(tmp_path / "o.sqlite")
    common.build_sqlite(db, schema, [(str(data), "t")])
    rows = sqlite3.connect(db).execute("SELECT a, b FROM t ORDER BY a").fetchall()
    assert rows == [("x", 1), ("y", 2)]
