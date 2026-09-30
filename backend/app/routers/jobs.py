from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.db_models import User
from app.processing import process_job
from app.repositories import job_outputs, jobs, uploads
from app.schemas import CreateJobRequest

router = APIRouter()


def serialize_job(job, outputs):
    job_id = job.id
    status = job.status
    upload_id = job.upload_id

    output_urls = {}
    for output in outputs:
        output_urls[output.stem] = f"/jobs/{job_id}/outputs/{output.stem}"
    serialized_job = {
        "id": job_id,
        "status": status,
        "upload_id": upload_id,
        "outputs": output_urls,
    }
    return serialized_job


@router.post("/jobs")
def create_job_endpoint(
    upload_request: CreateJobRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    session: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    upload_id = upload_request.upload_id
    upload = uploads.get_upload(session, upload_id)
    if upload is None or current_user.id != upload.user_id:
        raise HTTPException(status_code=404, detail="The upload does not exist")
    job = jobs.create_job(session, upload_id)
    job_id = job.id
    session.commit()

    background_tasks.add_task(
        process_job,
        job_id,
        request.app.state.model_session,
        request.app.state.db_session_factory,
    )

    return serialize_job(job, [])


@router.get("/jobs/{job_id}")
def get_job_endpoint(
    job_id: UUID,
    session: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = jobs.get_job(session, job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail="The requested job does not exist",
        )
    upload = uploads.get_upload(session, job.upload_id)
    if upload is None or upload.user_id != current_user.id:
        raise HTTPException(
            status_code=404,
            detail="The requested job does not exist",
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
    job = jobs.get_job(session, job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail="The job does not exist",
        )

    upload = uploads.get_upload(session, job.upload_id)
    if upload is None or upload.user_id != current_user.id:
        raise HTTPException(
            status_code=404,
            detail="The job does not exist",
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
