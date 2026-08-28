"""Create and populate the demo table in a Databricks catalog/schema.

    export DATABRICKS_HOST=dbc-xxxx.cloud.databricks.com
    export DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/abc123
    export DATABRICKS_TOKEN=dapi...
    export DATABRICKS_CATALOG=main          # optional, default 'main'
    export DATABRICKS_SCHEMA=default        # optional, default 'default'
    python seed.py

Requires the Databricks driver:  pip install "databricks-sql-connector[sqlalchemy]"

NOTE: seeding writes; the app itself only reads. Give the app a read-only token and use a
write-capable one (or the Databricks UI) just for this one-time seed.
"""

import os

from sqlalchemy import create_engine, text

from app import conn_url  # reuse the same URL builder

ROWS = [
    ("Sample1", 11, 13, 14, 15, "A", "X"),
    ("Sample2", 25, 16, 17, 18, "A", "X"),
    ("Sample3", 12, 9, 10, 11, "A", "Y"),
    ("Sample4", 22, 23, 25, 26, "B", "Y"),
    ("Sample5", 15, 24, 24, 25, "B", "Z"),
    ("Sample6", 21, 11, 17, 18, "B", "Z"),
    ("Sample7", 19, 20, 13, 16, "C", "X"),
    ("Sample8", 28, 14, 22, 12, "C", "Z"),
]


def main():
    engine = create_engine(conn_url(), future=True)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS expression"))
        conn.execute(text(
            "CREATE TABLE expression (sample STRING, geneA INT, geneB INT, "
            "geneC INT, geneD INT, category STRING, grp STRING)"
        ))
        conn.execute(
            text("INSERT INTO expression VALUES "
                 "(:s, :a, :b, :c, :d, :cat, :grp)"),
            [dict(s=r[0], a=r[1], b=r[2], c=r[3], d=r[4], cat=r[5], grp=r[6]) for r in ROWS],
        )
    engine.dispose()
    print("Wrote %d rows to %s" % (len(ROWS), os.environ["DATABRICKS_HOST"]))


if __name__ == "__main__":
    main()
