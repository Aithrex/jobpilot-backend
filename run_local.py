"""
Local development runner for the Aithrex resume/jobs backend.

This mirrors the path wiring that the original portfolio monorepo did in its
root `local.py`: the service code lives at the repo root (app.py, blueprints/,
services/, schemas/) while the shared utilities (utils.*, models.*) live under
`shared/python/`. We add `shared/python` to sys.path so `from utils.config
import AppConfig` and friends resolve, then hand off to the standard Flask app
factory in app.py.

Usage:
    python run_local.py

Routes are served at http://localhost:5000/api/*

Auth note: this service only *validates* JWTs. It must share the same
JWT_SECRET_KEY as the portfolio auth service that mints them (set it in .env).
"""

import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Shared layer (utils.*, models.*) — must be on the path before importing app.
sys.path.insert(0, str(ROOT / "shared" / "python"))
# Repo root so `blueprints`, `services`, `schemas` resolve as top-level packages.
sys.path.insert(0, str(ROOT))

# Load .env from the repo root if python-dotenv is available.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from app import create_app  # noqa: E402  (import after sys.path setup)

app = create_app()

if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "true").lower() == "true"
    port = int(os.getenv("PORT", "5000"))
    app.run(debug=debug, host="0.0.0.0", port=port)
