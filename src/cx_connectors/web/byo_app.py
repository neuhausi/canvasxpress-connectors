"""FastAPI factory for the bring-your-own-database app.

    from cx_connectors.web.byo_app import create_byo_app
    app = create_byo_app()          # reads SESSION_SECRET / ENCRYPTION_KEY from env
    # uvicorn yourmodule:app

Each user logs in, registers their own database (connection string stored encrypted),
and charts their own data via ``/api/data``. Users are isolated by the session cookie.
Requires the ``web`` and ``sql`` extras.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from ..reshape import rows_to_cx
from ..sources.sql import ReadOnlyViolation, SqlSource
from ..store import Store

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


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
    store = store or Store(db_path, encryption_key)

    app = FastAPI(title="canvasxpress-connectors · BYO database")
    app.add_middleware(
        SessionMiddleware, secret_key=session_secret, same_site="lax", https_only=https_only
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
        conn_url = (body.get("conn_url") or "").strip()
        sql = (body.get("sql") or "").strip()
        if not (name and conn_url and sql):
            raise HTTPException(status_code=400, detail="name, conn_url and sql are required")
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
            header, rows = SqlSource(record["conn_url"], record["sql"]).read()
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
