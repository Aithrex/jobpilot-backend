# One image for the API and the worker. Built on the box (linux/arm64).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY shared/python/requirements.txt ./deps/requirements-shared.txt
COPY infra/compose/requirements.txt ./deps/requirements.txt
RUN pip install --no-cache-dir -r deps/requirements.txt

COPY shared ./shared
COPY services ./services
COPY config ./config
COPY infra/compose/gunicorn.conf.py infra/compose/aws-config ./infra/compose/

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001
