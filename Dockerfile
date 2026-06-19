# lingua_patch — Telegram long-polling worker.
FROM python:3.12-slim

# ffmpeg is required to convert mp3 audio into Telegram OGG/Opus voice notes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persist the SQLite DB + audio on a mounted volume (e.g. Railway volume at /data).
ENV DB_PATH=/data/bot.db \
    MEDIA_DIR=/data/media \
    PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
