# canvasxpress-connectors

[![CI](https://github.com/neuhausi/canvasxpress-connectors/actions/workflows/ci.yml/badge.svg)](https://github.com/neuhausi/canvasxpress-connectors/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/canvasxpress-connectors)](https://pypi.org/project/canvasxpress-connectors/)
[![Python versions](https://img.shields.io/pypi/pyversions/canvasxpress-connectors)](https://pypi.org/project/canvasxpress-connectors/)
[![License: MIT](https://img.shields.io/pypi/l/canvasxpress-connectors)](LICENSE)

Feed [CanvasXpress](https://www.canvasxpress.org/) from **authenticated** data sources —
databases and Google Sheets — by reshaping query results into CanvasXpress data objects
served from **your own origin**. The browser never holds a credential.

```
Browser (CanvasXpress)  ──►  your app (this package)  ──►  authenticated source
   no secrets                 auth + encrypted creds        DB / Google Sheets
```

![Example CanvasXpress bar chart rendered from data reshaped by canvasxpress-connectors](docs/screenshot.png)

## Install

```bash
pip install canvasxpress-connectors            # core only (needs just cryptography)
pip install "canvasxpress-connectors[sql]"     # + SQLAlchemy databases
pip install "canvasxpress-connectors[sheets]"  # + Google Sheets
pip install "canvasxpress-connectors[all]"     # everything incl. the web app
```

## The 3-second version

Any source returns `(header, rows)`; `rows_to_cx` turns that into a CanvasXpress object:

```python
from cx_connectors.sources import SqlSource
from cx_connectors.sources.base import to_cx

data = to_cx(SqlSource(
    "sqlite:///demo.db",
    'SELECT sample, GeneA, GeneB, category AS "Category" FROM expression',
))
# {"y": {"vars": ["GeneA","GeneB"], "smps": [...], "data": [...]}, "x": {"Category": [...]}}
```

Return `data` as JSON from an endpoint; the page does `new CanvasXpress("cx", data, {...})`.

## Architecture

| Layer | Module | Job |
|-------|--------|-----|
| Reshape | `cx_connectors.reshape` | rows → CanvasXpress `{y, x}` (core, no heavy deps) |
| Sources | `cx_connectors.sources` | `DataSource` protocol + `SqlSource`, `GoogleSheetsSource` |
| Store | `cx_connectors.store` | users (PBKDF2) + per-user **encrypted** connection strings |
| Web | `cx_connectors.web` | `create_byo_app()` (database, login) · `create_sheets_app()` (Google Sheets, OAuth) — mountable FastAPI apps |

Adding a backend (BigQuery, a REST API, CSV) = one class with a `read()` returning
`(header, rows)`. Nothing else changes.

## Runnable demo — bring-your-own-database, with login

Each user logs in, registers **their own** database (connection string stored
encrypted), and charts **their own** data. Users are isolated by session.

```bash
pip install -e ".[all]"
export ENCRYPTION_KEY=$(python -c "from cx_connectors.store import generate_key;print(generate_key())")
export SESSION_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
python examples/seed_demo.py     # users alice & bob, each with their own SQLite DB
python examples/run_byo.py       # http://localhost:8100
```

Log in as `alice`/`alicepw` and `bob`/`bobpw` (incognito) — each sees only their own rows.

## Runnable demo — Google Sheets, per-user OAuth

Each user connects **their own** Google account; the app reads **their** private sheet.
The browser never sees a Google token or URL.

Prereqs: a Google OAuth **Web application** client (Cloud Console → Credentials) with
redirect URI `http://localhost:8080/oauth/callback`, and the **Google Sheets API** enabled.

```bash
pip install -e ".[all]"
export GOOGLE_CLIENT_ID=...  GOOGLE_CLIENT_SECRET=...
export OAUTH_REDIRECT_URI=http://localhost:8080/oauth/callback
export SESSION_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
export TOKEN_ENCRYPTION_KEY=$(python -c "from cx_connectors.store import generate_key;print(generate_key())")
export OAUTHLIB_INSECURE_TRANSPORT=1     # localhost http only; remove in production
python examples/run_sheets.py            # http://localhost:8080 → Connect Google Sheets
```

## Use it inside your own FastAPI

```python
from cx_connectors.web import create_byo_app, create_sheets_app
app = create_byo_app(https_only=True)      # database app: login + per-user DBs
# or
app = create_sheets_app(https_only=True)   # Google Sheets app: per-user OAuth
```

Or just the pieces — call `SqlSource` / `GoogleSheetsSource` + `rows_to_cx` from your
own handlers, and bring your own auth.

## Databases beyond SQLite

Connection strings are SQLAlchemy URLs; add the driver and go:

- Postgres: `pip install "psycopg[binary]"` → `postgresql+psycopg://user:pw@host/db`
- MySQL: `pip install PyMySQL` → `mysql+pymysql://user:pw@host/db`

## Parameterized queries (live-data controls)

A source's SQL can declare `:name` bind parameters, and `GET /api/data?source=…&name=…`
forwards matching request params into them — **only** the params the query declares,
always as **bound** parameters (never string-interpolated), so the browser can supply
values but never alter the query shape. An unknown/extra request key is ignored; a
declared param absent from the request is bound as `NULL`, so the `(:name IS NULL OR …)`
idiom lets a control "widen" back to everything.

```python
store.save_source(
    "alice", "sales",
    "sqlite:///file:/srv/data/sales.sqlite?mode=ro&uri=true",
    'SELECT sample, revenue FROM sales '
    'WHERE (:region IS NULL OR region = :region) '
    '  AND (:q IS NULL OR product LIKE :q) '
    'ORDER BY sample',
)
```

Then `/api/data?source=sales&region=EMEA` binds `region="EMEA"`, `q=NULL`. This is what a
[canvasxpress-dashboards](https://github.com/neuhausi/canvasxpress-dashboards) `mode:"param"`
control drives: a source `"query": { "region": "$region", "q": "$q" }` maps the dashboard
parameters onto these bind names. For a `LIKE` search, wrap the value with `%` in SQL —
`'%' || :q || '%'` — rather than in the browser.

To register many sources for a deployment at once (instead of the `/connectors` UI), use
[`examples/_tools/register_sources.py`](examples/_tools/register_sources.py) with a JSON
config (`examples/_tools/sources.example.json`) — the DB files stay on the server; only the
encrypted URL + SQL are stored, and it prints each source's declared bind params.

## Security notes

- Connection strings / tokens are **Fernet-encrypted at rest**; passwords are PBKDF2-hashed.
- `SqlSource` enforces a single read-only `SELECT`; still give the DB user least-privilege read access.
- **Query params are bound, never interpolated**, and only the `:name` binds the SQL declares
  are forwarded — an injected `?foo=…` key never reaches the database.
- For production: HTTPS + `https_only=True` cookies, rate-limit `/auth/login`, secrets from a
  manager (not `.env`), and pool engines per source.

## Deploying the BYO-database demo behind a reverse-proxy subpath (canvasxpress.org)

The demo runs as a plain localhost uvicorn service exposed by Apache under a
path prefix — the same pattern as the
[canvasxpress-mcp](https://github.com/neuhausi/canvasxpress-mcp) server. The
demo UI (`src/cx_connectors/web/static/index.html`) derives its API base from
the path it is served under, so it works at `/` in development and at
`/connectors/` in production. The deployment at
`https://www.canvasxpress.org/connectors/` was set up exactly as follows
(2026-08-20).

### 1 — Install (as the site user)

```bash
ssh canvasxpress@canvasxpress.org -p 7822
git clone https://github.com/neuhausi/canvasxpress-connectors.git
cd canvasxpress-connectors
python3 -m venv .venv
.venv/bin/pip install -e '.[all]'
```

### 2 — Persistent secrets + demo seed

Secrets live in `examples/byo_database/.env` (gitignored, `chmod 600`) so the
encrypted connection strings survive restarts:

```bash
cd examples/byo_database
EK=$(../../.venv/bin/python -c 'from cx_connectors.store import generate_key;print(generate_key())')
SS=$(../../.venv/bin/python -c 'import secrets;print(secrets.token_urlsafe(32))')
printf 'ENCRYPTION_KEY=%s\nSESSION_SECRET=%s\nHTTPS_ONLY=1\nMOUNT_PREFIX=/connectors\n' "$EK" "$SS" > .env
chmod 600 .env
../../.venv/bin/python seed_demo.py     # users alice/alicepw & bob/bobpw
```

`HTTPS_ONLY=1` marks the session cookie Secure (the app sits behind Apache
TLS). The cookie is named `cxc_session` so it can't collide with the co-hosted
canvasxpress-dashboards app (`cxd_session`).

`MOUNT_PREFIX` is needed because cPanel's LiteSpeed (which parses the Apache
config) forwards the request path **unstripped** through a
`ProxyPass`-in-`<Location>` — the backend receives `/connectors/...`, not
`/...`. With the prefix set, `examples/byo_database/app.py` mounts the app at
both `/` and `/connectors`, so it works direct and proxied. (On genuine Apache
httpd, which strips the matched prefix, you can omit it.)

### 3 — Run it on port 8300

Port **8300** avoids the MCP server (8100) and dashboards (8200) on the same
host. A `server.sh` in the repo root wraps uvicorn:

```bash
cd ~/canvasxpress-connectors
./server.sh start        # stop | restart | status
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8300/   # → 200
```

Logs and pidfile land in `examples/byo_database/`. It does **not** auto-start
on reboot; add a crontab entry if you want that:

```
@reboot /home/canvasxpress/canvasxpress-connectors/server.sh start
```

### 4 — Expose it through Apache (as root, one time)

Shared with the dashboards app — one userdata include, staged at
`~canvasxpress/dashboards-connectors-proxy.conf`:

```apache
# Trailing-slash URLs are required: the UI derives its API base from the prefix.
RedirectMatch ^/connectors$ /connectors/

<Location /connectors>
    PassengerEnabled Off
    ProxyPass http://127.0.0.1:8300
    ProxyPassReverse http://127.0.0.1:8300
</Location>
```

```bash
cp ~canvasxpress/dashboards-connectors-proxy.conf \
   /etc/apache2/conf.d/userdata/ssl/2_4/canvasxpress/canvasxpress.org/
/scripts/ensure_vhost_includes --user=canvasxpress
/scripts/restartsrv_httpd
```

The demo is then live at `https://www.canvasxpress.org/connectors/`
(log in as `alice`/`alicepw` or `bob`/`bobpw`).

To update the deployment: `git pull && ./server.sh restart` (re-run
`pip install -e '.[all]'` if dependencies changed).

## Contributing

Development setup, linting/tests, how to add a new data source, and the release process
are in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT

