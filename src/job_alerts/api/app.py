"""The FastAPI application factory.

Serves the JSON API and, when a built SPA is present, the static frontend too
(the single-container fallback — the two-service Docker setup lets nginx serve
it instead). Unlike the old Gradio app there is **no LLM startup gate**: browsing
stored jobs needs no LLM, so the API is up immediately and only translation and
new searches depend on the tunnel, degrading gracefully when it is down.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .routes import router


class SpaStaticFiles(StaticFiles):
    """StaticFiles with a single-page-app fallback.

    A client-side route like `/profile` or `/jobs/xyz` is neither a file nor a
    directory on disk, so plain `StaticFiles` 404s on a hard load or reload —
    `html=True` only serves index.html for directories. Serving index.html for any
    unmatched, non-asset path lets React Router take over, which is what makes deep
    links and refreshes work. Real missing assets (a path with a file extension)
    still 404 honestly.
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and "." not in path.rsplit("/", 1)[-1]:
                return await super().get_response("index.html", scope)
            raise

# In dev the Vite server (default :5173) calls the API cross-origin. Overridable
# so a deployment can widen or lock this down.
_DEFAULT_DEV_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


def _cors_origins() -> list[str]:
    raw = os.environ.get("JOB_ALERTS_CORS_ORIGINS", "").strip()
    if not raw:
        return _DEFAULT_DEV_ORIGINS
    return [o.strip() for o in raw.split(",") if o.strip()]


def create_app(*, static_dir: str | os.PathLike[str] | None = None) -> FastAPI:
    app = FastAPI(
        title="LabScout API",
        version="1.0.0",
        summary="JSON API over the LabScout pipeline for the React dashboard.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)

    # Optionally serve the built SPA. `SpaStaticFiles` falls back to index.html
    # for unmatched client-side routes (/profile, /jobs/xyz) so deep links and
    # reloads work; the /api router is matched first because it is included above
    # this mount.
    static = static_dir or os.environ.get("JOB_ALERTS_STATIC_DIR")
    if static:
        path = Path(static)
        if path.is_dir():
            app.mount("/", SpaStaticFiles(directory=str(path), html=True), name="spa")

    return app
