# Bring-your-own-database → CanvasXpress (multi-tenant, with login)

The advanced database example: each user **logs into the app**, registers **their own**
database connection (stored encrypted), and charts **their own** data. Users are
isolated by session. Uses `cx_connectors.web.create_byo_app`.

```bash
# from the repo root, once:
pip install -e ".[all]"

cd examples/byo_database
export ENCRYPTION_KEY=$(python -c "from cx_connectors.store import generate_key;print(generate_key())")
export SESSION_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
python seed_demo.py            # users alice & bob, each with their own SQLite DB
uvicorn app:app --port 8100    # open http://localhost:8100
```

Log in as `alice`/`alicepw` and, in an incognito window, `bob`/`bobpw` — each sees only
their own rows.

> Note: `seed_demo.py` and `app.py` must share the same `ENCRYPTION_KEY` (so the app can
> decrypt what the seed stored). Export it once, as above, before running either.

## What it shows

- Password login (PBKDF2) + signed-session cookie gate every data call.
- Per-user **encrypted** connection strings (`Store`).
- Isolation: `/api/data` only reads the sources owned by the logged-in user.
- Read-only guard: a registered query must be a single `SELECT`.

This is the database analogue of the Google Sheets OAuth example — there each user
authenticates to Google; here each user authenticates to your app, which holds their DB
credentials.
