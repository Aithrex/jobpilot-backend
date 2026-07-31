# JobPilot auto-apply Playwright worker (GCP browser-execution plane, P5).
#
# Official Playwright Python base — Chromium + all OS deps preinstalled, so the
# image is reproducible and we never fight missing shared libraries. Pinned to a
# specific Playwright version so the browser build and the pip package match.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Deps first for layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY worker.py ats_adapter.py ./

# Drop privileges — the base image ships a non-root `pwuser`.
USER pwuser

EXPOSE 8080

# One request = one browser (Cloud Run concurrency is pinned to 1). A single
# gunicorn worker with a generous timeout matches the Cloud Run request timeout.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "1", \
     "--timeout", "900", "worker:app"]
