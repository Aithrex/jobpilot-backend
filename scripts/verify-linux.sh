#!/usr/bin/env bash
# Run the backend verification suite inside the CI-like Python 3.12 image.
# Use this when a Windows/macOS renderer result disagrees with GitHub Actions
# (bundled Charis SIL should match; this path also matches Ubuntu fontconfig).
#
# Requires Docker. Does not use production credentials.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="${VERIFY_IMAGE:-python:3.12.14-bookworm}"
echo "verify-linux: image=$IMAGE"
exec docker run --rm \
  -e ENVIRONMENT=development \
  -e USE_SSM_SECRETS=false \
  -e PYTHONPATH=/src/shared/python \
  -v "$ROOT":/src -w /src \
  "$IMAGE" \
  bash -lc 'python -m pip install --upgrade pip && pip install -r shared/python/requirements.txt -r requirements-dev.txt && python scripts/verify.py --full'
