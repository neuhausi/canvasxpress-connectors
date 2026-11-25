"""Create example.parquet with a small demo table (via DuckDB).

    python seed.py
"""

import os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "example.parquet")


def main():
    import duckdb

    con = duckdb.connect()
    con.execute("SELECT setseed(0.42)")
    con.execute(
        """
        COPY (
          SELECT
            ['EMEA','AMER','APAC','LATAM'][(cast(random()*4 as int) % 4) + 1] AS region,
            ['Laptop','Phone','Tablet','Monitor'][(cast(random()*4 as int) % 4) + 1] AS product,
            round(20 + random() * 1980, 2) AS amount
          FROM range(50000)
        ) TO '{0}' (FORMAT PARQUET)
        """.format(OUT)
    )
    n = con.execute("SELECT count(*) FROM read_parquet('{0}')".format(OUT)).fetchone()[0]
    print("Wrote %d rows to %s" % (n, OUT))


if __name__ == "__main__":
    main()
