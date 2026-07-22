"""FastAPI factory for the Google Sheets app (per-user OAuth).

    from cx_connectors.web.sheets_app import create_sheets_app
    app = create_sheets_app()      # reads GOOGLE_CLIENT_ID/SECRET, OAUTH_REDIRECT_URI,
                                   # SESSION_SECRET, TOKEN_ENCRYPTION_KEY from env

Each browser user connects THEIR OWN Google account once (OAuth consent). We store
their encrypted refresh token server-side, keyed by a signed-session uid. The browser
never sees a Google token or URL -- it calls /api/sheet-data, which reads that user's
private sheet and returns a CanvasXpress data object. Requires the ``web`` and
``sheets`` extras.
"""

from __future__ import annotations

import os
import uuid
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from ..store import TokenStore
from ..reshape import rows_to_cx
from ..sources.google_sheets import GoogleSheetsSource

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static_sheets")

DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


def _client_config(client_id, client_secret, redirect_uri):
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }


def create_sheets_app(
    token_store: Optional[TokenStore] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    redirect_uri: Optional[str] = None,
    session_secret: Optional[str] = None,
    encryption_key: Optional[str] = None,
    db_path: Optional[str] = None,
    scopes: Optional[List[str]] = None,
    https_only: bool = False,
    serve_static: bool = True,
) -> FastAPI:
    client_id = client_id or os.environ["GOOGLE_CLIENT_ID"]
    client_secret = client_secret or os.environ["GOOGLE_CLIENT_SECRET"]
    redirect_uri = redirect_uri or os.environ["OAUTH_REDIRECT_URI"]
    session_secret = session_secret or os.environ["SESSION_SECRET"]
    encryption_key = encryption_key or os.environ["TOKEN_ENCRYPTION_KEY"]
    db_path = db_path or os.getenv("TOKEN_DB_PATH", "tokens.db")
    scopes = scopes or DEFAULT_SCOPES
    token_store = token_store or TokenStore(db_path, encryption_key)
    client_config = _client_config(client_id, client_secret, redirect_uri)

    app = FastAPI(title="canvasxpress-connectors · Google Sheets")
    app.add_middleware(
        SessionMiddleware, secret_key=session_secret, same_site="lax", https_only=https_only
    )

    def uid(request: Request) -> str:
        u = request.session.get("uid")
        if not u:
            u = uuid.uuid4().hex
            request.session["uid"] = u
        return u

    def make_flow(state=None):
        from google_auth_oauthlib.flow import Flow
        return Flow.from_client_config(
            client_config, scopes=scopes, redirect_uri=redirect_uri, state=state
        )

    # ---- OAuth ----
    @app.get("/oauth/login")
    def oauth_login(request: Request):
        uid(request)
        flow = make_flow()
        auth_url, state = flow.authorization_url(
            access_type="offline", include_granted_scopes="true", prompt="consent"
        )
        request.session["oauth_state"] = state
        return RedirectResponse(auth_url)

    @app.get("/oauth/callback")
    def oauth_callback(request: Request):
        expected = request.session.get("oauth_state")
        if not expected or request.query_params.get("state") != expected:
            raise HTTPException(status_code=400, detail="OAuth state mismatch")
        flow = make_flow(state=expected)
        flow.fetch_token(authorization_response=str(request.url))
        creds = flow.credentials
        if not creds.refresh_token:
            raise HTTPException(
                status_code=400,
                detail="No refresh token returned. Revoke prior access and reconnect.",
            )
        email = None
        try:
            from googleapiclient.discovery import build
            info = build("oauth2", "v2", credentials=creds).userinfo().get().execute()
            email = info.get("email")
        except Exception:
            pass
        token_store.save(
            user_id=uid(request), refresh_token=creds.refresh_token,
            token_uri=creds.token_uri, client_id=client_id,
            client_secret=client_secret, scopes=scopes, email=email,
        )
        return RedirectResponse("/")

    @app.post("/oauth/disconnect")
    def oauth_disconnect(request: Request):
        token_store.delete(uid(request))
        return {"connected": False}

    @app.get("/api/status")
    def status(request: Request):
        rec = token_store.load(uid(request))
        return {"connected": bool(rec), "email": rec["email"] if rec else None}

    # ---- data ----
    @app.get("/api/sheet-data")
    def sheet_data(request: Request, spreadsheetId: str, range: str = "A1:Z1000"):
        rec = token_store.load(uid(request))
        if not rec:
            raise HTTPException(status_code=401, detail="Not connected to Google")
        from google.oauth2.credentials import Credentials
        creds = Credentials(
            token=None, refresh_token=rec["refresh_token"], token_uri=rec["token_uri"],
            client_id=rec["client_id"], client_secret=rec["client_secret"],
            scopes=rec["scopes"],
        )
        try:
            header, rows = GoogleSheetsSource(creds, spreadsheetId, range).read()
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Sheets API error: %s" % exc)
        try:
            return JSONResponse(rows_to_cx(header, rows))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    if serve_static:
        app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")

    return app
