#!/usr/bin/env bash
# Boot: prepare the volume, create tables, seed audios, then serve.
set -e

mkdir -p var/uploads var/reels var/validated

# Seed the grading history into the volume on first boot (idempotent — only if absent).
if [ ! -f var/grades.jsonl ] && [ -f corpus/grades.jsonl ]; then
  cp corpus/grades.jsonl var/grades.jsonl
  echo "[start] seeded grading history into var/grades.jsonl"
fi

# Create tables + apply column migrations (idempotent). Don't crash the boot if the DB isn't ready.
python -m app.migrate || echo "[start] migrate warning (continuing)"

# Seed the curated audio library (idempotent; R2 upload is best-effort and may warn).
python -m app.seed.seed_audio || echo "[start] seed_audio warning (continuing)"

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
