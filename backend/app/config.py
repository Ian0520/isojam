import os
from datetime import timedelta

JWT_SECRET_KEY = os.environ["ISOJAM_JWT_SECRET_KEY"]
ACCESS_TOKEN_EXPIRES_DELTA = timedelta(minutes=30)