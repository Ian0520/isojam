from app.repositories import jobs, uploads, users, job_outputs
from app.storage import save_upload
from app.processing import process_job
from app.model import create_model_session
from app.database import get_db, SessionLocal
from app.schemas import RegisterRequest, UserResponse
from app.security import hash_password

from fastapi import FastAPI, UploadFile, HTTPException, BackgroundTasks, Request, Depends
from pathlib import Path
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel
from contextlib import asynccontextmanager
from uuid import UUID

ALLOWED_AUDIO_TYPES = {
    "audio/wav",
    "audio/mpeg",
}

class CreateJobRequest(BaseModel):
    upload_id: UUID

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



def create_app(model_session_factory=create_model_session, 
               db_session_factory=SessionLocal,
               ):
    @asynccontextmanager
    async def lifespan(app: FastAPI):      
        session = model_session_factory()
        app.state.model_session = session

        try:
            yield
        finally:
            session.close()

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/uploads")
    def upload_file(audio_file: UploadFile, session: Session = Depends(get_db)):
        # Check if type is allowed
        if audio_file.content_type not in ALLOWED_AUDIO_TYPES:
            raise HTTPException(
                status_code=415,
                detail="Unsupported audio type",
            )

        saved_path = save_upload(audio_file)

        upload_record = uploads.create_upload(session, audio_file.filename, saved_path.name)
        upload_id = upload_record.id
        session.commit()

        return {
            "id": upload_id,
            "filename": audio_file.filename,
            "content_type": audio_file.content_type,
        }

    @app.post("/jobs")
    def create_job_endpoint(
            upload_request: CreateJobRequest,
            background_tasks: BackgroundTasks,
            request: Request,
            session: Session = Depends(get_db),
    ):
        upload_id = upload_request.upload_id
        upload = uploads.get_upload(session, upload_id)
        if upload is None:
            raise HTTPException(
                status_code=404,
                detail="The upload does not exist"
            )
        job = jobs.create_job(session, upload_id)
        job_id = job.id
        session.commit()
        
        background_tasks.add_task(
            process_job,
            job_id,
            request.app.state.model_session,
            db_session_factory,
        )

        return serialize_job(job, [])

    @app.get("/jobs/{job_id}")
    def get_job_endpoint(job_id: UUID, session: Session = Depends(get_db)):
        job = jobs.get_job(session, job_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail="The requested job does not exist",
            )
        outputs = job_outputs.get_job_outputs(session, job_id)
        return serialize_job(job, outputs)

    @app.get("/jobs/{job_id}/outputs/{stem}")
    def download_job_output(job_id: UUID, stem: str, session: Session = Depends(get_db)):
        job = jobs.get_job(session, job_id)
        if job is None:
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

    @app.post(
        "/register",
        response_model=UserResponse,
        status_code=201,
    )
    def register_endpoint(request: RegisterRequest, session: Session = Depends(get_db)):
        normalized_email = str(request.email).strip().lower()
        existing_user = users.get_user_by_email(session, normalized_email)
        if existing_user is not None:
            raise HTTPException(
                status_code=409,
                detail="Email is already registered",
            )
        password_hash = hash_password(request.password)
        try: 
            user = users.create_user(
                session,
                normalized_email,
                password_hash,
            )
            session.commit()
        except IntegrityError:
            session.rollback()
            raise 409
        return UserResponse(
            id=user.id,
            email=user.email,
        )
    return app


app = create_app()

