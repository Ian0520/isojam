from fastapi import FastAPI, UploadFile, HTTPException, BackgroundTasks, Request
from app.storage import save_upload
from app.jobs import create_job, get_job
from app.uploads import create_upload, get_upload
from app.processing import process_job
from pydantic import BaseModel
from contextlib import asynccontextmanager
from app.model import create_model_session
from pathlib import Path
from fastapi.responses import FileResponse

ALLOWED_AUDIO_TYPES = {
    "audio/wav",
    "audio/mpeg",
}

class CreateJobRequest(BaseModel):
    upload_id: str


def create_app(model_session_factory=create_model_session):
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
    def upload_file(audio_file: UploadFile):
        # Check if type is allowed
        if audio_file.content_type not in ALLOWED_AUDIO_TYPES:
            raise HTTPException(
                status_code=415,
                detail="Unsupported audio type",
            )

        saved_path = save_upload(audio_file)

        upload_record = create_upload(audio_file.filename, saved_path.name)
        upload_id = upload_record["id"]
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
    ):
        upload_id = upload_request.upload_id
        upload = get_upload(upload_id)
        if upload is None:
            raise HTTPException(
                status_code=404,
                detail="The upload does not exist"
            )
        job = create_job(upload_id)
        
        background_tasks.add_task(
            process_job,
            job["id"],
            request.app.state.model_session,
        )

        return job

    @app.get("/jobs/{job_id}")
    def get_job_endpoint(job_id: str):
        job = get_job(job_id)
        if job is not None:
            return job
        else:
            raise HTTPException(
                status_code=404,
                detail="The requested job does not exist",
            )

    @app.get("/jobs/{job_id}/outputs/{stem}")
    def download_job_output(job_id: str, stem: str):
        job = get_job(job_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail="The job does not exist",
            )

        if job["status"] != "completed":
            raise HTTPException(
                status_code=409,
                detail="The job is not completed",
            )

        outputs = job.get("outputs", {})
        
        if stem not in outputs:
            raise HTTPException(
                status_code=404,
                detail="The requested stem does not exist",
            )

        output_path = Path(outputs[stem])

        if not output_path.is_file():
            raise HTTPException(
                        status_code=404,
                        detail="The output file is missing",
                    )
            
        return FileResponse(output_path)
        
    return app


app = create_app()

