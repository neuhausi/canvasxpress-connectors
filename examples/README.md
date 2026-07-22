# Examples

Each folder is a self-contained, runnable example. Install the package once from the
repo root, then `cd` into an example and follow its README.

```bash
pip install -e ".[all]"
```

| Example | Source | Auth | Start here for… |
|---------|--------|------|-----------------|
| [`sqlite/`](sqlite/) | SQLite file | none | the smallest possible database → CanvasXpress |
| [`postgres/`](postgres/) | Postgres server | none | the same, against a real DB (driver swap only) |
| [`google_sheets/`](google_sheets/) | Google Sheets | per-user OAuth | each user charts their own private sheet |
| [`byo_database/`](byo_database/) | any SQLAlchemy DB | app login | each user registers & charts their own database |

The first two show the **data path** (source → `to_cx` → `/api/data` → chart). The last
two add **authentication** — OAuth for Google, app login for databases.

All four produce the same shape of output: a CanvasXpress data object, served from your
own origin, that the browser hands to `new CanvasXpress(...)`.
