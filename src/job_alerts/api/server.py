"""uvicorn launcher for the JSON API. Reached via `python -m job_alerts serve`."""

from __future__ import annotations

import argparse
import os


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m job_alerts serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument(
        "--static-dir",
        default=os.environ.get("JOB_ALERTS_STATIC_DIR"),
        help="serve a built React SPA from this directory (single-container mode)",
    )
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev)")
    args = parser.parse_args(argv)

    import uvicorn

    from .app import create_app

    if args.reload:
        # reload needs an import string, not an app instance.
        os.environ.setdefault("JOB_ALERTS_STATIC_DIR", args.static_dir or "")
        uvicorn.run("job_alerts.api.factory:app", host=args.host, port=args.port, reload=True)
    else:
        uvicorn.run(create_app(static_dir=args.static_dir), host=args.host, port=args.port)
    return 0
