"""FastAPI factory for the bring-your-own-database app.

    from cx_connectors.web.byo_app import create_byo_app
    app = create_byo_app()          # reads SESSION_SECRET / ENCRYPTION_KEY from env
    # uvicorn yourmodule:app

Each user logs in, registers their own database (connection string stored encrypted),
and charts their own data via ``/api/data``. Users are isolated by the session cookie.
Requires the ``web`` and ``sql`` extras.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from ..reshape import rows_to_cx
from ..sources.packed import PackedMatrixSource
from ..sources.salesforce import ReadOnlyViolation as SoqlReadOnlyViolation
from ..sources.salesforce import SalesforceSource
from ..sources.servicenow import ServiceNowSource, servicenow_oauth_token
from ..sources.sql import ReadOnlyViolation, SqlSource, bind_param_names
from ..store import Store

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


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


def _read_saas_source(record: dict):
    """Build the Salesforce/ServiceNow source for a stored record and read it.

    The record's ``conn_url`` holds the decrypted JSON auth/query blob. For ServiceNow with
    OAuth, a short-lived bearer token is minted per read; the password/refresh credentials are
    what's stored, never the access token.

    :param record: A ``store.get_source`` record with ``kind`` ``"salesforce"``/``"servicenow"``.
    :returns: ``(header, rows)`` from the source's ``read()``.
    """
    kind = record.get("kind")
    if kind == "salesforce":
        auth = json.loads(record["conn_url"]) if record["conn_url"] else {}
        return SalesforceSource(record["sql"], **auth).read()

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


def create_byo_app(
    store: Optional[Store] = None,
    session_secret: Optional[str] = None,
    encryption_key: Optional[str] = None,
    db_path: Optional[str] = None,
    allow_signup: Optional[bool] = None,
    https_only: bool = False,
    serve_static: bool = True,
) -> FastAPI:
    session_secret = session_secret or os.environ["SESSION_SECRET"]
    encryption_key = encryption_key or os.environ["ENCRYPTION_KEY"]
    db_path = db_path or os.getenv("APP_DB_PATH", "app.db")
    if allow_signup is None:
        allow_signup = os.getenv("ALLOW_SIGNUP", "1") == "1"
    https_only = https_only or os.getenv("HTTPS_ONLY", "0") == "1"
    store = store or Store(db_path, encryption_key)

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
            if not soql:
                raise HTTPException(status_code=400, detail="soql is required")
            try:
                SalesforceSource(soql, client=object())  # validates read-only, no connect
            except SoqlReadOnlyViolation as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            store.save_source(user, name, json.dumps(auth), soql, kind="salesforce")
            return {"sources": store.list_sources(user)}

        if kind == "servicenow":
            cfg = _servicenow_config_from_body(body)
            store.save_source(user, name, json.dumps(cfg), "", kind="servicenow")
            return {"sources": store.list_sources(user)}

        # Default: a plain SQL SELECT source.
        conn_url = (body.get("conn_url") or "").strip()
        sql = (body.get("sql") or "").strip()
        if not (conn_url and sql):
            raise HTTPException(status_code=400, detail="conn_url and sql are required")
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
                    record["conn_url"], cfg["table"], cfg["value_col"], cfg.get("template_key"),
                    name_col=cfg.get("name_col", "name"),
                    json_table=cfg.get("json_table", "json"),
                    genes=genes, max_genes=cfg.get("max_genes", 200),
                    template_col=cfg.get("template_col", "str"),
                    template_key_col=cfg.get("template_key_col", "key"),
                    value_encoding=cfg.get("value_encoding", "json"),
                    value_sep=cfg.get("value_sep", ";"),
                )
                return JSONResponse(src.read_cx())

            # SaaS/REST sources: run the SOQL query / Table API read and reshape.
            if record.get("kind") in ("salesforce", "servicenow"):
                header, rows = _read_saas_source(record)
                return JSONResponse(rows_to_cx(header, rows))

            sql = record["sql"]
            # Forward request query params to the SQL, but ONLY the ones the query
            # explicitly declares as `:name` bind parameters — and always as bound
            # parameters, never string-interpolated. A declared param absent from
            # the request is passed as NULL so a query can widen with the
            # `(:name IS NULL OR col = :name)` idiom; any extra/unknown request key
            # is ignored. This keeps the browser unable to alter the query shape.
            declared = bind_param_names(sql)
            params = {name: request.query_params.get(name) for name in declared}
            header, rows = SqlSource(record["conn_url"], sql, params).read()
            return JSONResponse(rows_to_cx(header, rows))
        except ReadOnlyViolation as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Database error: %s" % exc)

    if serve_static:
        app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")

    return app
