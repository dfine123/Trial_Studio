FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# System deps: ffmpeg/ffprobe (QC + decode), libGL/glib (OpenCV), libsndfile (librosa).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

ENV PORT=8000
EXPOSE 8000

# Single service: run migrations, start the background indexer (worker), then serve the API.
# The worker runs in the background; if it can't reach Redis it won't take the API down.
CMD ["sh", "-c", "alembic upgrade head && { python -m app.workers.run & } && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
