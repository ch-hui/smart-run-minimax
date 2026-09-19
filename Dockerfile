# syntax=docker/dockerfile:1.6
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy application source.
COPY app.py ./
COPY run.sh ./

EXPOSE 8000

# Defaults — can be overridden by docker-compose / runtime env.
ENV HOST=0.0.0.0 \
    PORT=8000

CMD ["sh", "-c", "uvicorn app:app --host ${HOST} --port ${PORT}"]