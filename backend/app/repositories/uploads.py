from app.db_models import Upload

def create_upload(session, 
                  original_filename, 
                  stored_filename,
                  user_id,
                  ):
    upload = Upload(
        user_id=user_id,
        original_filename=original_filename,
        stored_filename=stored_filename,
    )
    session.add(upload)
    session.flush()
    return upload


def get_upload(session, upload_id):
    return session.get(Upload, upload_id)
    