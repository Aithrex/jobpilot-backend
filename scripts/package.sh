#!/usr/bin/env bash
# Package each microservice into a deployable Lambda zip.
#
# Each function bundles ONLY its own service dir (app.py, lambda_handler.py,
# blueprints/, services/). All shared code + deps come from the layer built by
# build_layer.sh, so these zips stay tiny.
#
# Produces:  build/auth.zip  build/jobs.zip  build/resume.zip  build/billing.zip
#
# Usage:  bash scripts/package.sh [svc ...]   (default: all 4)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICES=("$@")
if [ ${#SERVICES[@]} -eq 0 ]; then
  SERVICES=(auth jobs resume billing)
fi

mkdir -p "$ROOT/build"

for svc in "${SERVICES[@]}"; do
  SRC="$ROOT/services/$svc"
  ZIP="$ROOT/build/$svc.zip"
  if [ ! -d "$SRC" ]; then
    echo "!! skip: services/$svc not found" >&2
    continue
  fi
  echo ">> Packaging $svc -> build/$svc.zip"
  rm -f "$ZIP"
  # Zip the CONTENTS of the service dir at the zip root (handler = lambda_handler.handler).
  # Exclude bytecode and the requirements file (deps live in the layer).
  if command -v zip >/dev/null 2>&1; then
    ( cd "$SRC" && zip -qr "$ZIP" . -x '*__pycache__*' -x '*.pyc' -x 'requirements.txt' )
  else
    # Portable fallback (no `zip` binary, e.g. Windows Git-Bash)
    python - "$SRC" "$ZIP" <<'PYZIP'
import os, sys, zipfile
src, out = sys.argv[1], sys.argv[2]
skip = {"requirements.txt"}
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".pyc") or f in skip:
                continue
            full = os.path.join(root, f)
            z.write(full, os.path.relpath(full, src))
PYZIP
  fi
  SIZE=$(du -h "$ZIP" | cut -f1)
  echo "   done ($SIZE)"
done

echo ">> All packages in build/"
