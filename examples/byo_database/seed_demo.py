"""Seed two demo users, each owning a SEPARATE SQLite database, to show isolation.

  alice / alicepw -> alice_data.db   (source "my-genes")
  bob   / bobpw   -> bob_data.db     (source "my-genes")

Requires ENCRYPTION_KEY to match the app run (put both in a .env). For the zero-config
demo, run_byo.py mints an ephemeral key — in that mode, seed and run share state only
if you set ENCRYPTION_KEY yourself first.
"""

import os
import sqlite3

from dotenv import load_dotenv

from cx_connectors.store import Store, generate_key

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DB = os.getenv("APP_DB_PATH", "app.db")
os.environ.setdefault("ENCRYPTION_KEY", generate_key())
ENCRYPTION_KEY = os.environ["ENCRYPTION_KEY"]

DEMO = {
    "alice": {"password": "alicepw", "db": os.path.join(HERE, "alice_data.db"),
              "rows": [("Sample1", 11, 13, 14, 15, "A", "X"),
                       ("Sample2", 25, 16, 17, 18, "A", "X"),
                       ("Sample3", 12, 9, 10, 11, "A", "Y"),
                       ("Sample4", 22, 23, 25, 26, "B", "Y")]},
    "bob": {"password": "bobpw", "db": os.path.join(HERE, "bob_data.db"),
            "rows": [("Ctrl1", 30, 5, 20, 8, "ctrl", "P"),
                     ("Ctrl2", 28, 7, 19, 9, "ctrl", "P"),
                     ("Treat1", 6, 31, 4, 27, "treat", "Q"),
                     ("Treat2", 8, 29, 5, 26, "treat", "Q")]},
}

SQL = ('SELECT sample, GeneA, GeneB, GeneC, GeneD, '
       'category AS "Category", grp AS "Group" FROM expression ORDER BY sample')


def make_user_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE IF EXISTS expression")
    conn.execute("CREATE TABLE expression (sample TEXT PRIMARY KEY, GeneA INT, GeneB INT, "
                 "GeneC INT, GeneD INT, category TEXT, grp TEXT)")
    conn.executemany("INSERT INTO expression VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def main():
    store = Store(APP_DB, ENCRYPTION_KEY)
    for username, info in DEMO.items():
        make_user_db(info["db"], info["rows"])
        store.create_user(username, info["password"])
        store.save_source(username, "my-genes", "sqlite:///" + info["db"], SQL)
        print("Seeded %-6s -> sqlite:///%s" % (username, info["db"]))
    print("\nENCRYPTION_KEY=%s" % ENCRYPTION_KEY)
    print("Export that (and a SESSION_SECRET) before run_byo.py so state matches.")


if __name__ == "__main__":
    main()
