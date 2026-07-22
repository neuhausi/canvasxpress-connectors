"""Create example.db (SQLite) with a small demo table.

    python seed.py
"""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "example.db")

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
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DROP TABLE IF EXISTS expression")
    conn.execute(
        "CREATE TABLE expression (sample TEXT PRIMARY KEY, GeneA INT, GeneB INT, "
        "GeneC INT, GeneD INT, category TEXT, grp TEXT)"
    )
    conn.executemany("INSERT INTO expression VALUES (?,?,?,?,?,?,?)", ROWS)
    conn.commit()
    conn.close()
    print("Wrote %d rows to %s" % (len(ROWS), DB_PATH))


if __name__ == "__main__":
    main()
