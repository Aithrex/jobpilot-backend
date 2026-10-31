"""Gunicorn for the gateway. Sized for 2 OCPU; raise the env values on a bigger box."""
import os

bind = "0.0.0.0:8000"
worker_class = "gthread"
workers = int(os.getenv("GUNICORN_WORKERS", "2"))
threads = int(os.getenv("GUNICORN_THREADS", "16"))
# Longer than nginx's keepalive to it, so nginx never reuses a closed socket.
keepalive = 75
timeout = 120
# A deploy's stop lets running async jobs finish. Keep it under the compose
# stop_grace_period, or Docker kills the container first.
graceful_timeout = 280
worker_tmp_dir = "/tmp"
accesslog = None
errorlog = "-"
