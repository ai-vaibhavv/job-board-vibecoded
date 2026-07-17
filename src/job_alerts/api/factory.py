"""Module-level app instance for `uvicorn ... --reload`, which needs an import
string rather than a constructed object. Production uses `create_app()` directly
(see `server.py`)."""

from __future__ import annotations

import os

from .app import create_app

app = create_app(static_dir=os.environ.get("JOB_ALERTS_STATIC_DIR") or None)
