"""The JSON endpoints. Every handler is a thin delegate to `service`.

Handlers that reach the database, the LLM or Discord are declared as plain
`def` (not `async def`) on purpose: the service layer opens a thread-bound
SQLite connection per call and uses `asyncio.run(...)` internally, which would
raise inside an already-running event loop. Starlette runs a sync handler in its
threadpool, giving each its own thread — exactly what that layer expects.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status

from ..dashboard import service as svc
from .jobs import TaskRegistry
from .schemas import PreviewRequest, PublishRequest, SearchRunRequest
from .security import require_write_auth

router = APIRouter(prefix="/api")

# One registry for the process. A search runs here so the request can return at
# once and the frontend can poll for progress.
_tasks = TaskRegistry()


# --- health & meta -------------------------------------------------------


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "llm_online": svc.llm_online()}


@router.get("/meta")
def meta() -> dict:
    return {
        "topics": svc.topic_choices(),
        "locations": svc.location_choices(),
        "sources": svc.source_choices(),
        "stats": svc.stats(),
    }


@router.get("/stats")
def stats() -> dict:
    return svc.stats()


# --- listing & detail ----------------------------------------------------


@router.get("/jobs")
def list_jobs(
    status: str | None = None,
    min_score: int | None = None,
    source: str | None = None,
    text: str | None = None,
    show_hidden: bool = False,
) -> dict:
    # "all" is the UI's word for no filter; the service wants None.
    status_arg = None if status in (None, "", "all") else status
    source_arg = None if source in (None, "", "all") else source
    jobs = svc.list_jobs_json(
        status=status_arg,
        min_score=min_score,
        source=source_arg,
        text=text,
        show_hidden=show_hidden,
    )
    return {"jobs": jobs, "total": len(jobs)}


# Job ids are `source:source_job_id`, and an RSS/URL-derived id contains slashes
# (e.g. `tum_hiwi:https://portal.mytum.de/...`). uvicorn decodes %2F back to "/",
# so a single-segment `{job_id}` never matches those — the request falls through
# to the SPA. The `:path` converter matches slashes; its regex backtracks to the
# trailing literal suffix (`/publish`, `/match`), so the sub-routes below still
# resolve. The greedy bare-detail route is therefore declared LAST, after every
# more specific `/jobs/...` route, or it would swallow them.


# --- mutations -----------------------------------------------------------


@router.post("/jobs/unhide-all", dependencies=[Depends(require_write_auth)])
def unhide_all() -> dict:
    return {"message": svc.unhide_all()}


@router.post("/jobs/{job_id:path}/publish", dependencies=[Depends(require_write_auth)])
def publish(job_id: str, body: PublishRequest) -> dict:
    return {"message": svc.publish_job(job_id, body.confirm)}


@router.post("/jobs/{job_id:path}/refresh", dependencies=[Depends(require_write_auth)])
def refresh(job_id: str) -> dict:
    # Fetch the real posting page: store the fuller description, act on the link
    # status (auto-hide a definitively dead one), (re)translate a German job.
    detail = svc.refresh_job_detail(job_id)
    if not detail.get("exists"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return detail


@router.post("/jobs/{job_id:path}/hide", dependencies=[Depends(require_write_auth)])
def hide(job_id: str) -> dict:
    return {"message": svc.hide_job(job_id)}


@router.get("/jobs/{job_id:path}/match")
def match(job_id: str) -> dict:
    """How the stored profile fits this opportunity (cache-first, may call the LLM)."""
    return svc.match_job(job_id)


@router.get("/jobs/{job_id:path}/tailoring")
def tailoring(job_id: str) -> dict:
    """Reversible suggestions for tailoring the résumé to this opportunity."""
    return svc.tailor_job(job_id)


# --- search --------------------------------------------------------------


@router.post("/search/preview", dependencies=[Depends(require_write_auth)])
def search_preview(body: PreviewRequest) -> dict:
    queries, scope = svc.preview_search(body.keywords, body.topics)
    return {"queries": queries, "scope": scope}


@router.post(
    "/search/run",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_write_auth)],
)
def search_run(body: SearchRunRequest) -> dict:
    def work() -> str:
        return svc.run_search(body.keywords, body.topics, body.locations)

    try:
        task = _tasks.start(work)
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A search is already running.",
        ) from None
    return task.as_dict()


@router.get("/search/run/{task_id}")
def search_status(task_id: str) -> dict:
    task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown task.")
    return task.as_dict()


@router.post("/resume", dependencies=[Depends(require_write_auth)])
def resume(file: UploadFile = File(...)) -> dict:
    # Persist the upload so the pypdf-based extractor can read it by path, then
    # bin it — the resume text is only ever sent to the LLM, never stored.
    suffix = Path(file.filename or "resume.pdf").suffix or ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(file.file.read())
        tmp_path = tmp.name
    try:
        keywords, topics, message = svc.prefill_from_resume(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return {"keywords": keywords, "topics": topics, "message": message}


# --- central academic profile (Phase 3) ----------------------------------

_MAX_RESUME_BYTES = 10 * 1024 * 1024  # 10 MB is plenty for a résumé; reject bombs.


@router.get("/profile")
def get_profile() -> dict:
    return svc.get_profile_json()


@router.post("/profile", dependencies=[Depends(require_write_auth)])
def upload_profile(file: UploadFile = File(...)) -> dict:
    """Upload a résumé: store the original immutably and extract a profile."""
    raw = file.file.read(_MAX_RESUME_BYTES + 1)
    if len(raw) > _MAX_RESUME_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "résumé exceeds 10 MB")
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty file")
    return svc.save_and_extract_profile(raw, file.filename or "resume", file.content_type)


@router.put("/profile", dependencies=[Depends(require_write_auth)])
def edit_profile(payload: dict) -> dict:
    try:
        return svc.update_profile(payload)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.delete("/profile", dependencies=[Depends(require_write_auth)])
def delete_profile() -> dict:
    return svc.delete_profile_data()


@router.get("/profile/export")
def export_profile() -> dict:
    return svc.export_profile()


@router.get("/profile/original")
def download_original() -> Response:
    """Download the immutable original résumé."""
    upload = svc.get_original_upload()
    if upload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no résumé stored")
    return Response(
        content=upload["raw"],
        media_type=upload["content_type"] or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{upload["filename"]}"'},
    )


# --- settings (secrets overlay) ------------------------------------------


@router.get("/settings", dependencies=[Depends(require_write_auth)])
def get_settings() -> dict:
    return svc.get_settings_status()


@router.post("/settings", dependencies=[Depends(require_write_auth)])
def save_settings(body: dict[str, str]) -> dict:
    # Only the keys the caller actually sent are touched; "" clears one back to
    # whatever .env provides. Unknown keys are ignored by the service.
    try:
        return svc.save_settings(body)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


# --- maintenance ---------------------------------------------------------


@router.post(
    "/maintenance/check-links",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_write_auth)],
)
def check_links() -> dict:
    # A polite request per job; runs in the background like a search so the
    # request returns at once and the UI polls /search/run/{task_id}.
    try:
        task = _tasks.start(svc.check_all_links)
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A background job (search or link check) is already running.",
        ) from None
    return task.as_dict()


# --- job detail (declared LAST) ------------------------------------------
# The greedy `{job_id:path}` would otherwise swallow every other `/jobs/...`
# route, so this catch-all must come after them all.


@router.get("/jobs/{job_id:path}")
def get_job(job_id: str) -> dict:
    detail = svc.job_detail_json(job_id)
    if not detail.get("exists"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return detail
