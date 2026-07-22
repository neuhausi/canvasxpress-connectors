"""Run the bring-your-own-database demo app.

    pip install -e ".[all]"
    python examples/seed_demo.py     # creates alice & bob + their own SQLite DBs
    python examples/run_byo.py       # http://localhost:8100

Demonstrates that the whole authenticated app is a few lines once the package does
the work.
"""

import os

from dotenv import load_dotenv
import uvicorn

from cx_connectors.web import create_byo_app

load_dotenv()

# Convenience for a first run: mint ephemeral secrets if none are set. Set real,
# persistent values in a .env for anything beyond a throwaway demo.
os.environ.setdefault("SESSION_SECRET", os.urandom(24).hex())
if "ENCRYPTION_KEY" not in os.environ:
    from cx_connectors.store import generate_key
    os.environ["ENCRYPTION_KEY"] = generate_key()

app = create_byo_app()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8100)
