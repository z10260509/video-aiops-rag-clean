FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend:/app

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt backend/requirements.txt
COPY worker/requirements.txt worker/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt -r worker/requirements.txt

COPY backend backend
COPY worker worker

CMD ["celery", "-A", "worker.app.celery_app:celery_app", "worker", "--loglevel=info", "--concurrency=1"]
