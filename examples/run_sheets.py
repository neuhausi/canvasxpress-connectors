"""Run the Google Sheets (per-user OAuth) demo app.

Prereqs:
  1. A Google OAuth *Web application* client (Google Cloud Console → Credentials),
     with authorized redirect URI  http://localhost:8080/oauth/callback
  2. Enable the Google Sheets API for the project.

    pip install -e ".[all]"
    export GOOGLE_CLIENT_ID=...            GOOGLE_CLIENT_SECRET=...
    export OAUTH_REDIRECT_URI=http://localhost:8080/oauth/callback
    export SESSION_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
    export TOKEN_ENCRYPTION_KEY=$(python -c "from cx_connectors.store import generate_key;print(generate_key())")
    export OAUTHLIB_INSECURE_TRANSPORT=1   # localhost http only; remove in production
    python examples/run_sheets.py          # http://localhost:8080
"""

import os

from dotenv import load_dotenv
import uvicorn

from cx_connectors.web import create_sheets_app

load_dotenv()

app = create_sheets_app()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
