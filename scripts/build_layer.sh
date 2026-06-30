#!/usr/bin/env bash
# Build the shared Lambda LAYER from shared/python/.
#
# The layer contains BOTH:
#   1. the shared CODE  (utils/ models/ services/ schemas/)  -> /opt/python/...
#   2. the installed DEPS (pymongo, fpdf2, cryptography, ...) -> /opt/python/...
# At runtime Lambda mounts the layer at /opt/python, which is on sys.path, so
# `from utils.config import ...` and `from services.resume_service import ...`
# resolve from the layer while each function bundles only its own service dir.
#
# Linux wheels are fetched explicitly (--platform manylinux2014_x86_64) so the
# layer is valid on the Lambda runtime even when built on Windows/macOS.
#
# Usage:  bash scripts/build_layer.sh [python-version]   (default 3.12)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYVER="${1:-3.12}"
OUT="$ROOT/build/layer"
PYDIR="$OUT/python"

echo ">> Cleaning $OUT"
rm -rf "$OUT" "$ROOT/build/layer.zip"
mkdir -p "$PYDIR"

echo ">> Copying shared code (utils, models, services, schemas)"
for d in utils models services schemas; do
  cp -r "$ROOT/shared/python/$d" "$PYDIR/$d"
done
# Strip bytecode caches from the copied code
find "$PYDIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

echo ">> Installing dependencies (Linux/manylinux wheels, cp${PYVER//./})"
python -m pip install \
  -r "$ROOT/shared/python/requirements.txt" \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version "$PYVER" \
  --only-binary=:all: \
  --upgrade \
  --target "$PYDIR"

echo ">> Zipping -> build/layer.zip"
if command -v zip >/dev/null 2>&1; then
  ( cd "$OUT" && zip -qr "$ROOT/build/layer.zip" python -x '*__pycache__*' )
else
  # Portable fallback (no `zip` binary, e.g. Windows Git-Bash)
  python - "$OUT" "$ROOT/build/layer.zip" <<'PYZIP'
import os, sys, zipfile
base, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(os.path.join(base, "python")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".pyc"):
                continue
            full = os.path.join(root, f)
            z.write(full, os.path.relpath(full, base))
PYZIP
fi

SIZE=$(du -h "$ROOT/build/layer.zip" | cut -f1)
echo ">> Done: build/layer.zip ($SIZE)"
