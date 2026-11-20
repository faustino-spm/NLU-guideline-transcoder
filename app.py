"""Entrypoint for hosts that look for a top-level `app` (Vercel), and for `uvicorn app:app`.

The app is built from environment variables (and `.env` when present) when this module is imported.
"""

from web import app_from_env

app = app_from_env()
