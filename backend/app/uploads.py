from uuid import uuid4

uploads = {}

def create_upload(original_filename, stored_filename):
    upload_id = str(uuid4())

    upload_record = {
        "id": upload_id,
        "original_filename": original_filename,
        "stored_filename": stored_filename,
    }

    uploads[upload_id] = upload_record

    return upload_record


def get_upload(upload_id):
    return uploads.get(upload_id)