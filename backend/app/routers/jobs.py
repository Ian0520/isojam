from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.db_models import Job, JobOutput, User
from app.processing import process_job
from app.repositories import job_outputs, jobs, uploads
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
) -> JobResponse:
    upload_id = upload_request.upload_id
    upload = uploads.get_upload(session, upload_id)
    if upload is None or current_user.id != upload.user_id:
        raise HTTPException(status_code=404, detail="The upload does not exist")
    job = jobs.create_job_with_limit(
        session,
        upload_id,
        current_user.id,
        max_unfinished_jobs=request.app.state.max_unfinished_jobs_per_user,
    )
    if job is None:
        session.rollback()
        raise HTTPException(
            status_code=429,
            detail="Unfinished job limit reached; wait for a job to finish",
        )
    job_id = job.id
    session.commit()

    background_tasks.add_task(
        process_job,
        job_id,
        request.app.state.model_session,
        request.app.state.db_session_factory,
    )

    return serialize_job(job, [])


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
