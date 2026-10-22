"""FastAPI factory for the bring-your-own-database app.

    from cx_connectors.web.byo_app import create_byo_app
    app = create_byo_app()          # reads SESSION_SECRET / ENCRYPTION_KEY from env
    # uvicorn yourmodule:app

Each user logs in, registers their own database (connection string stored encrypted),
and charts their own data via ``/api/data``. Users are isolated by the session cookie.
Requires the ``web`` and ``sql`` extras.

``APP_DB_PATH`` accepts either a bare SQLite file path (default ``app.db``) or a
database URL.  Pass a Postgres URL (``postgresql://user:pw@host:5432/db``) for
hosts that restart or run several processes — requires the ``postgres`` extra.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from ..connmask import MASK, unmask_conn_url
from ..databricks_oauth import (
    OAuthExchangeFailed,
    ViewerSessionRequired,
    resolve_conn_url,
)
from ..pushdown import PushdownError, parse_query, run_join, run_pushdown
from ..reshape import rows_to_cx
from ..sources.packed import PackedMatrixSource
from ..sources.pins_board import BOARD_TYPES as PINS_BOARD_TYPES
from ..sources.pins_board import PinsBoardSource
from ..sources.salesforce import ReadOnlyViolation as SoqlReadOnlyViolation
from ..sources.salesforce import SalesforceSource, salesforce_oauth_token
from ..sources.servicenow import ServiceNowSource, servicenow_oauth_token
from ..sources.simulated import SimulatedLiveSource
from ..sources.sql import ReadOnlyViolation, SqlSource, bind_param_names
from ..sqlstore import open_store
from ..store import Store
from .sse import sse_event_stream

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Bounds for the live-demo tick cadence (seconds): fast enough to look live, slow enough
# that a client cannot ask the server to spin. Target is "dashboard-live", not HFT.
_STREAM_MIN_INTERVAL = 0.1
_STREAM_MAX_INTERVAL = 60.0
_STREAM_MAX_VARS = 20


def _clamp_stream_interval(raw: Optional[str]) -> float:
    """Parse the ``interval`` query param into a bounded tick cadence in seconds.

    :param raw: The raw query value (or ``None``); non-numeric/absent falls back to 1s.
    :returns: A float clamped to ``[_STREAM_MIN_INTERVAL, _STREAM_MAX_INTERVAL]``.
    """
    try:
        interval = float(raw) if raw not in (None, "") else 1.0
    except (TypeError, ValueError):
        interval = 1.0
    return max(_STREAM_MIN_INTERVAL, min(_STREAM_MAX_INTERVAL, interval))


def _parse_stream_vars(raw: Optional[str]) -> list:
    """Parse the demo stream's ``vars`` param (comma-separated) into a bounded series list.

    :param raw: The raw query value (or ``None``); absent/empty falls back to a single series.
    :returns: A list of 1..``_STREAM_MAX_VARS`` variable names.
    """
    names = [v.strip() for v in (raw or "").split(",") if v.strip()]
    if not names:
        names = ["metric"]
    return names[:_STREAM_MAX_VARS]


def _open_demo_stream(query, interval: float, user: str) -> SimulatedLiveSource:
    """Open the built-in simulated metric feed (no upstream, no credential).

    :param query: The request's query params; ``vars`` names the series.
    :param interval: The clamped tick cadence in seconds.
    :param user: The signed-in user (unused: the demo has no per-user data).
    :returns: A fresh :class:`SimulatedLiveSource`.
    """
    return SimulatedLiveSource(variables=_parse_stream_vars(query.get("vars")), interval=interval)


# Live streams every app offers, by name. ``open(query_params, interval, user)`` builds the
# LiveSource for one subscription; hosts add their own via ``create_byo_app(live_streams=...)``.
DEFAULT_LIVE_STREAMS = {
    "demo": {
        "title": "Simulated metrics (demo)",
        "variables": ["cpu", "mem"],
        "query": "vars=cpu,mem",
        "open": _open_demo_stream,
    },
}


def _parse_stream_max_ticks(raw: Optional[str]) -> Optional[int]:
    """Parse the optional ``max`` param: end the demo stream after this many ticks.

    :param raw: The raw query value (or ``None``); absent/invalid/non-positive means open-ended.
    :returns: A positive int cap, or ``None`` for an unbounded stream.
    """
    try:
        value = int(raw) if raw not in (None, "") else 0
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _servicenow_config_from_body(body: dict) -> dict:
    """Normalize a ServiceNow registration body into the stored (encrypted) config.

    ``fields`` accepts a list or a comma-separated string; ``auth`` is passed through as
    ``{"type": "basic"|"oauth", ...}`` and kept in the encrypted blob with the query config.

    :param body: The POST body from the register-source form.
    :returns: The config dict to JSON-encode into ``conn_enc``.
    :raises HTTPException: If instance or table is missing.
    """
    instance = (body.get("instance") or "").strip()
    table = (body.get("table") or "").strip()
    if not (instance and table):
        raise HTTPException(status_code=400, detail="instance and table are required")
    fields = body.get("fields")
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.split(",") if f.strip()] or None
    limit = body.get("limit")
    return {
        "instance": instance,
        "table": table,
        "query": (body.get("query") or "").strip(),
        "fields": fields,
        "limit": int(limit) if limit not in (None, "", 0) else None,
        "auth": body.get("auth") or {},
    }


def _pins_config_from_body(body: dict) -> dict:
    """Normalize a pins-board registration body into the stored (encrypted) config.

    The stored blob carries the board backend, the pin name, and the per-backend
    parameters (a Connect ``api_key``, an S3 ``region``, a URL version ``path``, …), which
    may include credentials — so it lives in the encrypted ``conn_enc`` column like the
    other SaaS sources, never in plain config.

    :param body: The POST body from the register-source form.
    :returns: The config dict to JSON-encode into ``conn_enc``.
    :raises HTTPException: If the board type is unknown or a required field is missing.
    """
    board_type = (body.get("board_type") or "").strip().lower()
    if board_type not in PINS_BOARD_TYPES:
        raise HTTPException(status_code=400,
                            detail="board_type must be one of %s" % ", ".join(PINS_BOARD_TYPES))
    pin = (body.get("pin") or body.get("pin_name") or "").strip()
    if not pin:
        raise HTTPException(status_code=400, detail="pin (name) is required")
    board_cfg = dict(body.get("config") or {})
    # Validate required per-backend params up front (clear 400, not a later 502).
    try:
        PinsBoardSource(board_type, pin, board_cfg)._require_params()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"board_type": board_type, "pin": pin, "config": board_cfg}


# ---- Salesforce OAuth apps ----------------------------------------------------------------
# An OAuth app (My Domain, client id, client secret) is defined ONLY in the server's
# environment (CXC_SALESFORCE_APPS, a JSON object); a registered source just names one. A
# user can therefore never supply a domain or secret of their own, nor make the server send a
# secret to a host of their choosing, and no credential is stored in the sources table.
_SF_APPS_ENV = "CXC_SALESFORCE_APPS"
_SF_PASSWORD_AUTH_ENV = "CXC_SALESFORCE_PASSWORD_AUTH"
_SF_TOKEN_TTL = 600.0          # seconds a minted token is reused; Salesforce sessions last longer
_SF_TOKENS: dict = {}          # (domain, client_id) -> (expires_at, {"access_token", "instance_url"})


def _salesforce_apps() -> dict:
    """The operator-defined OAuth apps, keyed by name (read on each call so edits apply).

    :returns: ``{name: {"domain", "client_id", "client_secret", "users"}}``; ``{}`` when unset.
    :raises RuntimeError: If the environment value is not a JSON object of objects.
    """
    raw = (os.getenv(_SF_APPS_ENV) or "").strip()
    if not raw:
        return {}
    try:
        apps = json.loads(raw)
    except ValueError:
        raise RuntimeError("%s is not valid JSON" % _SF_APPS_ENV)
    if not isinstance(apps, dict) or not all(isinstance(app, dict) for app in apps.values()):
        raise RuntimeError("%s must be a JSON object of app name -> settings" % _SF_APPS_ENV)
    return apps


def _salesforce_app_allows(app: dict, user: str) -> bool:
    """Whether ``user`` may use ``app``: only when ``users`` lists them (or ``"*"``). Deny by default."""
    users = app.get("users")
    if isinstance(users, str):
        users = [users]
    return isinstance(users, list) and ("*" in users or user in users)


def _salesforce_app_for(user: str, name: str) -> dict:
    """Resolve an OAuth app for a user.

    An unknown app and one the user may not use are reported identically, so app names cannot
    be probed.

    :raises PermissionError: If the app does not exist or the user may not use it.
    :raises RuntimeError: If the operator's configuration is malformed or incomplete.
    """
    app = _salesforce_apps().get(name)
    if app is None or not _salesforce_app_allows(app, user):
        raise PermissionError("Salesforce OAuth app %r is not available to this user" % name)
    if not (app.get("domain") and app.get("client_id") and app.get("client_secret")):
        raise RuntimeError("Salesforce OAuth app %r needs domain, client_id and client_secret" % name)
    return app


def _salesforce_password_auth_allowed() -> bool:
    """Whether sources may be registered with a stored username/password/token (default yes)."""
    return (os.getenv(_SF_PASSWORD_AUTH_ENV) or "on").strip().lower() not in ("off", "0", "false", "no")


def _salesforce_token(app: dict, fresh: bool = False) -> dict:
    """A bearer token for an OAuth app, minted on demand and reused briefly.

    :param fresh: Skip the cache (used after Salesforce reports an expired session).
    """
    key = (app["domain"], app["client_id"])
    cached = _SF_TOKENS.get(key)
    if cached and not fresh and cached[0] > time.monotonic():
        return cached[1]
    token = salesforce_oauth_token(app["domain"], app["client_id"], app["client_secret"])
    _SF_TOKENS[key] = (time.monotonic() + _SF_TOKEN_TTL, token)
    return token


def _read_salesforce_oauth(soql: str, app: dict):
    """Run a SOQL query as an OAuth app, minting a token and retrying once if it has expired."""
    for attempt in (0, 1):
        token = _salesforce_token(app, fresh=bool(attempt))
        try:
            return SalesforceSource(soql, session_id=token["access_token"],
                                    instance_url=token["instance_url"]).read()
        except Exception as exc:
            # simple_salesforce raises SalesforceExpiredSession on a 401; by name so the
            # optional dependency is not imported here.
            if attempt == 0 and type(exc).__name__ == "SalesforceExpiredSession":
                continue
            raise


def authorize_saas_source(user: str, record: dict) -> None:
    """Raise if ``user`` may not read this stored source (a no-op for non-OAuth sources).

    Registration checks this once, but the operator can later narrow an app's ``users``; call
    it before every read so a revoked user stops reading immediately.

    :raises PermissionError: If the source names an OAuth app the user may no longer use.
    :raises RuntimeError: If that app's configuration is broken.
    """
    if record.get("kind") != "salesforce":
        return
    auth = json.loads(record["conn_url"]) if record.get("conn_url") else {}
    if auth.get("oauth_app"):
        _salesforce_app_for(user, auth["oauth_app"])


def _read_saas_source(record: dict, owner: Optional[str] = None):
    """Build the Salesforce / ServiceNow / pins source for a stored record and read it.

    The record's ``conn_url`` holds the decrypted JSON auth/query blob. For ServiceNow with
    OAuth, a short-lived bearer token is minted per read; the password/refresh credentials are
    what's stored, never the access token. A Salesforce source that names an ``oauth_app``
    stores no credential at all: the app's secret is looked up in the server environment and a
    token is minted from it.

    :param record: A ``store.get_source`` record with ``kind`` ``"salesforce"`` /
        ``"servicenow"`` / ``"pins"``.
    :param owner: The source's owner. When given, an OAuth app's ``users`` list is enforced
        here too (callers that already ran :func:`authorize_saas_source` may omit it).
    :returns: ``(header, rows)`` from the source's ``read()``.
    """
    kind = record.get("kind")
    if kind == "salesforce":
        auth = json.loads(record["conn_url"]) if record["conn_url"] else {}
        if auth.get("oauth_app"):
            if owner is not None:
                app = _salesforce_app_for(owner, auth["oauth_app"])
            else:
                app = _salesforce_apps().get(auth["oauth_app"])
                if not app or not (app.get("domain") and app.get("client_id")
                                   and app.get("client_secret")):
                    raise RuntimeError("Salesforce OAuth app %r is not configured" % auth["oauth_app"])
            return _read_salesforce_oauth(record["sql"], app)
        return SalesforceSource(record["sql"], **auth).read()

    if kind == "pins":
        cfg = json.loads(record["conn_url"]) if record["conn_url"] else {}
        return PinsBoardSource(cfg["board_type"], cfg["pin"], cfg.get("config")).read()

    cfg = json.loads(record["conn_url"]) if record["conn_url"] else {}
    auth = cfg.pop("auth", {}) or {}
    if (auth.get("type") or "basic") == "oauth":
        token = servicenow_oauth_token(
            cfg["instance"], auth["client_id"], auth["client_secret"],
            username=auth.get("username"), password=auth.get("password"),
            refresh_token=auth.get("refresh_token"),
        )
        return ServiceNowSource(oauth_token=token, **cfg).read()
    return ServiceNowSource(username=auth.get("username"), password=auth.get("password"),
                            **cfg).read()


def _resolve_or_http(stored_url, headers, memo=None):
    """Resolve a stored source URL for the current request (substituting the
    viewer's freshly-exchanged Databricks token for an opted-in source), mapping
    the viewer-OAuth failures to HTTP errors. Done BEFORE a handler's generic
    ``except Exception -> 502`` so the clear cause is not swallowed.

    :param stored_url: The source's stored connection URL.
    :param headers: The request headers (for the viewer session token).
    :param memo: Optional per-request ``{stored_url: resolved}`` cache (joins).
    :returns: The URL to connect with.
    :raises HTTPException: 403 when no viewer session token, 502 when the token
        exchange failed; neither message contains a token.
    """
    try:
        return resolve_conn_url(stored_url, headers, memo)
    except ViewerSessionRequired as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except OAuthExchangeFailed as exc:
        raise HTTPException(status_code=502, detail=str(exc))


def _pushdown_cx(header, rows, query):
    """CanvasXpress data for a pushdown result.

    A grouped result gets a row id made of its group values ("EMEA · 2026-01"),
    so every group column stays a real column (to color, facet and filter by).
    An empty result stays a valid (empty) object rather than an error, since a
    filter may match nothing.
    """
    groups = query.get("groupBy") or []
    if groups:
        header = ["id"] + list(header)
        rows = [[" · ".join(str(v) for v in r[:len(groups)])] + list(r) for r in rows]
    if not rows:
        measures = [h for h in header[1:] if h not in groups]
        return {"y": {"vars": measures, "smps": [], "data": [[] for _ in measures]}}
    return rows_to_cx(header, rows)


def create_byo_app(
    store: Optional[Store] = None,
    session_secret: Optional[str] = None,
    encryption_key: Optional[str] = None,
    db_path: Optional[str] = None,
    allow_signup: Optional[bool] = None,
    https_only: bool = False,
    serve_static: bool = True,
    live_streams: Optional[dict] = None,
) -> FastAPI:
    """Build the BYO-database app.

    :param live_streams: Extra live (SSE) streams to offer, ``name -> {"open", "title",
        "variables", "query"}``, merged over :data:`DEFAULT_LIVE_STREAMS`. ``open(query_params,
        interval, user)`` returns a :class:`~cx_connectors.sources.base.LiveSource`; it runs
        server-side, so any upstream credential stays here (``user`` lets it pick the viewer's
        own). ``query`` is a default query string the stream listing appends to its url.
    """
    session_secret = session_secret or os.environ["SESSION_SECRET"]
    encryption_key = encryption_key or os.environ["ENCRYPTION_KEY"]
    db_path = db_path or os.getenv("APP_DB_PATH", "app.db")
    if allow_signup is None:
        allow_signup = os.getenv("ALLOW_SIGNUP", "1") == "1"
    https_only = https_only or os.getenv("HTTPS_ONLY", "0") == "1"
    store = store or open_store(db_path, encryption_key)

    try:
        row_cap = max(1, int(os.getenv("CX_MAX_ROWS", "100000")))
    except ValueError:
        row_cap = 100000

    app = FastAPI(title="canvasxpress-connectors · BYO database")
    app.add_middleware(
        SessionMiddleware, secret_key=session_secret, same_site="lax", https_only=https_only,
        # Distinct name so co-hosted apps (e.g. dashboards) don't clobber it.
        session_cookie="cxc_session",
    )

    def require_user(request: Request) -> str:
        user = request.session.get("user")
        if not user:
            raise HTTPException(status_code=401, detail="Not logged in")
        return user

    # ---- auth ----
    @app.post("/auth/signup")
    async def signup(request: Request):
        if not allow_signup:
            raise HTTPException(status_code=403, detail="Signup disabled")
        body = await request.json()
        username, password = body.get("username", ""), body.get("password", "")
        if len(username) < 3 or len(password) < 6:
            raise HTTPException(status_code=400, detail="Username ≥3 and password ≥6 chars")
        if not store.create_user(username, password):
            raise HTTPException(status_code=409, detail="Username already taken")
        request.session["user"] = username
        return {"user": username}

    @app.post("/auth/login")
    async def login(request: Request):
        body = await request.json()
        username, password = body.get("username", ""), body.get("password", "")
        if not store.check_user(username, password):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        request.session["user"] = username
        return {"user": username}

    @app.post("/auth/logout")
    async def logout(request: Request):
        request.session.clear()
        return {"user": None}

    @app.get("/auth/me")
    def me(request: Request):
        return {"user": request.session.get("user")}

    # ---- per-user sources ----
    @app.get("/api/sources")
    def list_sources(request: Request):
        return {"sources": store.list_sources(require_user(request))}

    @app.get("/api/salesforce/apps")
    def salesforce_apps(request: Request):
        """The Salesforce OAuth apps this user may register a source against (names only —
        never a domain, client id or secret), and whether a stored password login is accepted."""
        user = require_user(request)
        try:
            apps = _salesforce_apps()
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        return {"apps": sorted(name for name, config in apps.items()
                               if _salesforce_app_allows(config, user)),
                "password_auth": _salesforce_password_auth_allowed()}

    @app.post("/api/sources")
    async def add_source(request: Request):
        user = require_user(request)
        body = await request.json()
        name = (body.get("name") or "").strip()
        kind = (body.get("kind") or "sql").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")

        # SaaS/REST sources (Salesforce SOQL, ServiceNow Table API) have no connection
        # string — their auth + query are stored, encrypted, as a JSON blob in conn_enc.
        if kind == "salesforce":
            soql = (body.get("soql") or "").strip()
            auth = body.get("auth") or {}
            oauth_app = (body.get("oauth_app") or "").strip()
            if not soql:
                raise HTTPException(status_code=400, detail="soql is required")
            try:
                SalesforceSource(soql, client=object())  # validates read-only, no connect
            except SoqlReadOnlyViolation as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            if oauth_app:
                # An OAuth app is named, never described: its domain and secret live only in
                # the server environment, so nothing credential-like is accepted or stored.
                if auth:
                    raise HTTPException(status_code=400,
                                        detail="Use either oauth_app or auth, not both")
                try:
                    _salesforce_app_for(user, oauth_app)
                except PermissionError as exc:
                    raise HTTPException(status_code=403, detail=str(exc))
                except RuntimeError as exc:
                    raise HTTPException(status_code=500, detail=str(exc))
                store.save_source(user, name, json.dumps({"oauth_app": oauth_app}), soql,
                                  kind="salesforce")
                return {"sources": store.list_sources(user)}
            if not _salesforce_password_auth_allowed():
                raise HTTPException(
                    status_code=403,
                    detail="This server does not accept a stored Salesforce username/password; "
                           "use oauth_app (see GET /api/salesforce/apps)")
            store.save_source(user, name, json.dumps(auth), soql, kind="salesforce")
            return {"sources": store.list_sources(user)}

        if kind == "servicenow":
            cfg = _servicenow_config_from_body(body)
            store.save_source(user, name, json.dumps(cfg), "", kind="servicenow")
            return {"sources": store.list_sources(user)}

        if kind == "pins":
            cfg = _pins_config_from_body(body)
            store.save_source(user, name, json.dumps(cfg), "", kind="pins")
            return {"sources": store.list_sources(user)}

        # Default: a plain SQL SELECT source.
        conn_url = (body.get("conn_url") or "").strip()
        sql = (body.get("sql") or "").strip()
        if not (conn_url and sql):
            raise HTTPException(status_code=400, detail="conn_url and sql are required")
        if MASK in conn_url:
            # An edit form shows a stored password as "********" and sends it back unchanged.
            # Restore the real one from the source being edited (the same name) or copied
            # (``secret_from``) — always one of THIS user's own — and only while the server
            # is unchanged, so the secret cannot be redirected somewhere new.
            original = store.get_source(user, (body.get("secret_from") or "").strip() or name)
            stored = original["conn_url"] if original and (original.get("kind") or "sql") == "sql" else None
            try:
                conn_url = unmask_conn_url(conn_url, stored)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        try:
            SqlSource(conn_url, sql)  # validates read-only without connecting
        except ReadOnlyViolation as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        store.save_source(user, name, conn_url, sql)
        return {"sources": store.list_sources(user)}

    @app.delete("/api/sources/{name}")
    def delete_source(request: Request, name: str):
        user = require_user(request)
        store.delete_source(user, name)
        return {"sources": store.list_sources(user)}

    # ---- data ----
    @app.get("/api/data")
    def data(request: Request, source: str):
        user = require_user(request)
        record = store.get_source(user, source)
        if not record:
            raise HTTPException(status_code=404, detail="No such source for this user")
        # A Salesforce source that names an OAuth app may be revoked after registration; check
        # now, outside the catch-all below, so a revoked user gets a real 403 (not a 502).
        try:
            authorize_saas_source(user, record)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        # Resolve a viewer-OAuth Databricks URL to a freshly-tokened one up front,
        # BEFORE the generic `except -> 502` below, so a missing-viewer 403 or a
        # token-exchange error surfaces its real cause. SaaS sources (salesforce /
        # servicenow) keep a JSON blob in `conn_url`, not a DB URL — never resolve it.
        conn_url = (record["conn_url"] if record.get("kind") in ("salesforce", "servicenow", "pins")
                    else _resolve_or_http(record["conn_url"], request.headers))
        try:
            # A 'packed' source reassembles a column-store matrix (CCLE/TCGA
            # expression) into a CanvasXpress object; its gene list comes from a
            # request param named by the config (default 'genes', comma-separated).
            if record.get("kind") == "packed":
                cfg = record.get("config") or {}
                gene_param = cfg.get("gene_param", "genes")
                raw = request.query_params.get(gene_param) or ""
                genes = [g.strip() for g in raw.split(",") if g.strip()]
                src = PackedMatrixSource(
                    conn_url, cfg["table"], cfg["value_col"], cfg.get("template_key"),
                    name_col=cfg.get("name_col", "name"),
                    json_table=cfg.get("json_table", "json"),
                    genes=genes, max_genes=cfg.get("max_genes", 200),
                    template_col=cfg.get("template_col", "str"),
                    template_key_col=cfg.get("template_key_col", "key"),
                    value_encoding=cfg.get("value_encoding", "json"),
                    value_sep=cfg.get("value_sep", ";"),
                )
                return JSONResponse(src.read_cx())

            # SaaS/REST sources: run the SOQL query / Table API / pin read and reshape.
            if record.get("kind") in ("salesforce", "servicenow", "pins"):
                header, rows = _read_saas_source(record, owner=user)
                return JSONResponse(rows_to_cx(header, rows))

            sql = record["sql"]
            # A pushdown query (`_q`, JSON) runs aggregation/filtering/limits in
            # the database around the owner's SELECT (see cx_connectors.pushdown).
            pushdown = request.query_params.get("_q")
            if pushdown:
                declared = bind_param_names(sql)
                params = {name: request.query_params.get(name) for name in declared}
                query = parse_query(pushdown)
                header, rows, truncated = run_pushdown(
                    conn_url, sql, params, query, row_cap)
                return JSONResponse(_pushdown_cx(header, rows, query),
                                    headers={"X-Cx-Rows": str(len(rows)),
                                             "X-Cx-Truncated": "1" if truncated else "0"})
            # Forward request query params to the SQL, but ONLY the ones the query
            # explicitly declares as `:name` bind parameters — and always as bound
            # parameters, never string-interpolated. A declared param absent from
            # the request is passed as NULL so a query can widen with the
            # `(:name IS NULL OR col = :name)` idiom; any extra/unknown request key
            # is ignored. This keeps the browser unable to alter the query shape.
            declared = bind_param_names(sql)
            params = {name: request.query_params.get(name) for name in declared}
            # Cap the raw read the same as pushdown/join: a saved SELECT * over a
            # huge table would otherwise fetchall() the whole thing into memory.
            source = SqlSource(conn_url, sql, params)
            header, rows = source.read(row_cap=row_cap)
            return JSONResponse(rows_to_cx(header, rows),
                                headers={"X-Cx-Rows": str(len(rows)),
                                         "X-Cx-Truncated": "1" if source.truncated else "0"})
        except (ReadOnlyViolation, PushdownError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Database error: %s" % exc)

    @app.get("/api/join")
    def join(request: Request, left: str, right: str, on: str = "smps", how: str = "inner",
             right_name: Optional[str] = None):
        """Join two of the user's SQL sources in their database (same connection),
        optionally aggregated with a pushdown query (``_q``)."""
        user = require_user(request)
        # One memo across both sides so two viewer-OAuth sources on the same
        # database resolve to the SAME token string (run_join requires the two
        # connection URLs to be equal). Resolution is before the 502 wrap below.
        memo = {}
        sides = []
        for name in (left, right):
            record = store.get_source(user, name)
            if not record:
                raise HTTPException(status_code=404, detail="No such source: %s" % name)
            if record.get("kind") not in (None, "", "sql"):
                raise HTTPException(status_code=400, detail="Only SQL sources can be joined here")
            sql = record["sql"]
            params = {n: request.query_params.get(n) for n in bind_param_names(sql)}
            conn_url = _resolve_or_http(record["conn_url"], request.headers, memo)
            sides.append((conn_url, sql, params))
        try:
            query = parse_query(request.query_params.get("_q") or "{}")
            header, rows, truncated = run_join(sides[0], sides[1], on, how, right_name or right,
                                               query, row_cap)
        except (ReadOnlyViolation, PushdownError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Database error: %s" % exc)
        return JSONResponse(_pushdown_cx(header, rows, query),
                            headers={"X-Cx-Rows": str(len(rows)),
                                     "X-Cx-Truncated": "1" if truncated else "0"})

    @app.get("/api/columns")
    def columns(request: Request, source: str):
        """A SQL source's columns with a sampled type (``number`` or ``text``),
        for building pushdown queries."""
        user = require_user(request)
        record = store.get_source(user, source)
        if not record:
            raise HTTPException(status_code=404, detail="No such source for this user")
        if record.get("kind") not in (None, "", "sql"):
            raise HTTPException(status_code=400, detail="Only SQL sources support pushdown")
        sql = record["sql"]
        params = {name: request.query_params.get(name) for name in bind_param_names(sql)}
        conn_url = _resolve_or_http(record["conn_url"], request.headers)
        try:
            header, rows, _ = run_pushdown(conn_url, sql, params,
                                           {"limit": 200}, 200)
        except (ReadOnlyViolation, PushdownError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Database error: %s" % exc)
        out = []
        for i, name in enumerate(header):
            values = [r[i] for r in rows if r[i] is not None]
            numeric = bool(values) and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                           for v in values)
            out.append({"name": name, "type": "number" if numeric else "text"})
        return {"columns": out}

    # ---- live streaming (SSE) ----
    streams = dict(DEFAULT_LIVE_STREAMS)
    streams.update(live_streams or {})

    @app.get("/api/streams")
    def list_streams(request: Request):
        """The live streams this server offers, for a dashboard's source picker: each
        ``{name, title, url, variables}``, ``url`` relative to this app."""
        require_user(request)
        return {"streams": [
            {"name": name, "title": entry.get("title") or name,
             "url": "/api/stream/" + name + (("?" + entry["query"]) if entry.get("query") else ""),
             "variables": list(entry.get("variables") or [])}
            for name, entry in sorted(streams.items())
        ]}

    @app.get("/api/stream/{stream}")
    async def stream_live(request: Request, stream: str):
        """Server-Sent-Events stream of a named live source (``GET /api/streams`` lists them).

        The browser opens an ``EventSource('/api/stream/<name>')`` and each ``tick`` event is a
        CanvasXpress increment fed to ``instance.pushData(tick)``. The session cookie
        authenticates the stream (EventSource sends cookies automatically). Common query
        params: ``interval`` (seconds between ticks, clamped) and ``max`` (stop after N ticks;
        omit/0 for an open-ended stream); a source may read more (the demo takes ``vars``).
        """
        user = require_user(request)
        entry = streams.get(stream)
        if entry is None:
            raise HTTPException(status_code=404, detail="No such stream: %s" % stream)
        interval = _clamp_stream_interval(request.query_params.get("interval"))
        max_ticks = _parse_stream_max_ticks(request.query_params.get("max"))
        source = entry["open"](request.query_params, interval, user)
        return StreamingResponse(
            sse_event_stream(source, is_disconnected=request.is_disconnected,
                             max_ticks=max_ticks),
            media_type="text/event-stream",
            headers={
                # Never cache or let a proxy buffer an event stream.
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    if serve_static:
        app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")

    return app
