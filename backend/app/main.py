from fastapi import FastAPI, UploadFile

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/uploads")
def upload_file(audio_file: UploadFile):
    return {
        "filename": audio_file.filename,
        "content_type": audio_file.content_type,
    }