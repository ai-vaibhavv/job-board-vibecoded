"""Optional HTTP Basic auth for the write endpoints.

The Gradio app refused a public `--share` without `--auth` because the UI
exposes Discord publishing and the paid search API. The same danger applies
here, so the mutating routes (publish, run-search, resume upload) depend on
`require_write_auth`. When `JOB_ALERTS_API_AUTH` is unset the check is a no-op —
the default local, single-user deployment stays friction-free — but the moment
a credential is configured, those routes demand it.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

_ENV_VAR = "JOB_ALERTS_API_AUTH"
_basic = HTTPBasic(auto_error=False)


def _expected() -> tuple[str, str] | None:
    raw = os.environ.get(_ENV_VAR, "").strip()
    if not raw:
        return None
    user, sep, pw = raw.partition(":")
    if not sep or not user or not pw:
        raise RuntimeError(f"{_ENV_VAR} must be 'username:password'")
    return user, pw


def require_write_auth(
    credentials: HTTPBasicCredentials | None = Depends(_basic),
) -> None:
    """Guard a mutating route. No-op unless JOB_ALERTS_API_AUTH is set."""
    expected = _expected()
    if expected is None:
        return
    user, pw = expected
    ok = credentials is not None and (
        # compare_digest on both to avoid leaking which half was wrong via timing
        secrets.compare_digest(credentials.username, user)
        & secrets.compare_digest(credentials.password, pw)
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )
