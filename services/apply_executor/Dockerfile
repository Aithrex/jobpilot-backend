# CI note: building/pushing this image needs ECR perms on the CI deployer —
# see infra/terraform/apply_executor.tf (aws_iam_user_policy.ci_ecr).
#
# Container-image Lambda for the auto-apply browser fleet (Playwright + Chromium).
# Built + pushed to ECR by CI (see infra/APPLY_EXECUTOR_DEPLOY.md); Terraform
# points the Lambda at the :prod image tag.
#
# Base = the official Playwright Python image (Chromium + all system libs
# preinstalled and version-matched to the playwright pip package), plus the AWS
# Lambda Runtime Interface Client so it speaks the Lambda invoke protocol.
FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

# Lambda Runtime Interface Client + our runtime deps. IMPORTANT: install
# playwright into THIS python (the one awslambdaric runs) — the base image ships
# playwright under a different interpreter, so the handler's python can't import
# it otherwise. The system libs Chromium needs are already in the base image;
# `playwright install chromium` fetches the matching browser into
# PLAYWRIGHT_BROWSERS_PATH (a no-op if the base already has that revision).
RUN pip install --no-cache-dir awslambdaric boto3 pymongo requests fpdf2 python-docx pypdf cryptography playwright==1.58.0 \
    && python -m playwright install chromium

# Shared layer code (services.*, utils.*, schemas.*) on the import path, exactly
# as the real Lambda layer mounts it at /opt/python.
COPY shared/python /opt/python
ENV PYTHONPATH=/opt/python

# The handler.
COPY services/apply_executor/lambda_handler.py /var/task/lambda_handler.py
WORKDIR /var/task

# Chromium sandbox can't run under the Lambda exec environment; the executor
# already launches with --no-sandbox / --single-process. HOME must be writable
# (Lambda's only writable dir is /tmp) or Chromium fails to start its profile.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/tmp \
    XDG_CONFIG_HOME=/tmp \
    XDG_CACHE_HOME=/tmp

ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["lambda_handler.handler"]
