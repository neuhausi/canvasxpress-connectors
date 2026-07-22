"""Create and populate the demo table in a Postgres database.

    export DATABASE_URL=postgresql+psycopg://user:pw@localhost:5432/postgres
    python seed.py

Requires the psycopg driver:  pip install "psycopg[binary]"
"""

import os

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]

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
    engine = create_engine(DATABASE_URL, future=True)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS expression"))
        conn.execute(text(
            'CREATE TABLE expression (sample TEXT PRIMARY KEY, "GeneA" INT, "GeneB" INT, '
            '"GeneC" INT, "GeneD" INT, category TEXT, grp TEXT)'
        ))
        conn.execute(
            text('INSERT INTO expression VALUES '
                 '(:s, :a, :b, :c, :d, :cat, :grp)'),
            [dict(s=r[0], a=r[1], b=r[2], c=r[3], d=r[4], cat=r[5], grp=r[6]) for r in ROWS],
        )
    engine.dispose()
    print("Wrote %d rows to %s" % (len(ROWS), DATABASE_URL.split("@")[-1]))


if __name__ == "__main__":
    main()
