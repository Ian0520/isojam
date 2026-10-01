FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    ISOJAM_PROCESSING_MODE=disabled \
    ISOJAM_DATABASE_PATH=/var/lib/isojam/metadata/isojam.db \
    ISOJAM_AUDIO_STORAGE_DIR=/var/lib/isojam/audio

WORKDIR /opt/isojam

# Install pinned dependencies before copying source so code edits reuse this layer.
COPY requirements-api.txt ./
RUN python -m pip install pip==26.2.1 \
    && python -m pip install --require-hashes --only-binary=:all: -r requirements-api.txt

COPY pyproject.toml ./
COPY app/ ./app/
RUN python -m pip install --no-deps --no-build-isolation . \
    && python -m pip check

# Migration commands use the same image and database setting as the API.
COPY alembic.ini ./
COPY migrations/ ./migrations/

RUN groupadd --gid 10001 isojam \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin isojam \
    && mkdir -p /var/lib/isojam/metadata /var/lib/isojam/audio \
    && chown -R 10001:10001 /var/lib/isojam

USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
