# treelz.ai / Trial Studio — production image (Railway)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# System deps: ffmpeg (reel render + QC), fonts-noto-color-emoji (caption emoji),
# libgl1/libglib2.0-0 (opencv-headless), libsndfile1 (librosa/soundfile).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-noto-color-emoji \
        libgl1 \
        libglib2.0-0 \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# App + bundled read-only assets (corpus incl. the grades seed, fonts, samples/audio).
# Writable data lives on the Railway volume mounted at /app/var.
COPY . .
RUN chmod +x start.sh

ENV PORT=8000
EXPOSE 8000
CMD ["./start.sh"]
