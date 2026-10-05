from collections.abc import Sequence
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import commit_session, get_db
from app.db_models import Job, JobOutput, User
from app.processing import process_job
from app.repositories import job_outputs, job_submissions, jobs, uploads
from app.schemas import CreateJobRequest, JobResponse

router = APIRouter()


def _get_owned_job(
    session: Session,
    job_id: UUID,
    user_id: UUID,
    *,
    not_found_detail: str,
) -> Job:
    job = jobs.get_job(session, job_id)
    if job is not None:
        upload = uploads.get_upload(session, job.upload_id)
        if upload is not None and upload.user_id == user_id:
            return job
    raise HTTPException(status_code=404, detail=not_found_detail)


def serialize_job(job: Job, outputs: Sequence[JobOutput]) -> JobResponse:
    output_urls = {
        output.stem: f"/jobs/{job.id}/outputs/{output.stem}" for output in outputs
    }
    return JobResponse(
        id=job.id,
        status=job.status,
        upload_id=job.upload_id,
        outputs=output_urls,
    )


@router.post("/jobs", response_model=JobResponse)
def create_job_endpoint(
    upload_request: CreateJobRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    session: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
            description="Reuse for retries of one submission; use a new key for a new job.",
        ),
    ] = None,
) -> JobResponse:
    upload_id = upload_request.upload_id
    upload = uploads.get_upload(session, upload_id)
    if upload is None or current_user.id != upload.user_id:
        raise HTTPException(status_code=404, detail="The upload does not exist")
    try:
        if idempotency_key is not None:
            replay = job_submissions.get_replayed_job(
                session, current_user.id, upload_id, idempotency_key
            )
            if replay is not None:
                outputs = job_outputs.get_job_outputs(session, replay.id)
                return serialize_job(replay, outputs)
        if request.app.state.processing_mode == "disabled":
            raise HTTPException(
                status_code=503, detail="Audio processing is unavailable"
            )
        submission = job_submissions.create_submission(
            session,
            upload_id,
            current_user.id,
            key=idempotency_key,
            max_unfinished_jobs=request.app.state.max_unfinished_jobs_per_user,
            execution_backend=request.app.state.processing_mode,
        )
    except job_submissions.SubmissionConflictError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    if submission is None:
        session.rollback()
        raise HTTPException(
            status_code=429,
            detail="Unfinished job limit reached; wait for a job to finish",
        )
    job = submission.job
    job_id = job.id
    commit_session(session)

    if submission.created:
        if job.execution_backend == "local":
            background_tasks.add_task(
                process_job,
                job_id,
                request.app.state.model_session,
                request.app.state.db_session_factory,
            )
        return serialize_job(job, [])
    outputs = job_outputs.get_job_outputs(session, job_id)
    return serialize_job(job, outputs)


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job_endpoint(
    job_id: UUID,
    session: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> JobResponse:
    job = _get_owned_job(
        session,
        job_id,
        current_user.id,
        not_found_detail="The requested job does not exist",
    )
    outputs = job_outputs.get_job_outputs(session, job_id)
    return serialize_job(job, outputs)


@router.get("/jobs/{job_id}/outputs/{stem}")
def download_job_output(
    job_id: UUID,
    stem: str,
    session: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = _get_owned_job(
        session,
        job_id,
        current_user.id,
        not_found_detail="The job does not exist",
    )

    if job.status != "completed":
        raise HTTPException(
            status_code=409,
            detail="The job is not completed",
        )

    output = job_outputs.get_job_output(session, job_id, stem)

    if output is None:
        raise HTTPException(
            status_code=404,
            detail="The requested stem does not exist",
        )

    output_path = Path(output.path)

    if not output_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="The output file is missing",
        )

    return FileResponse(output_path)
