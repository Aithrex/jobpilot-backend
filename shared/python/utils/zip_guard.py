"""DOCX/ZIP archive guard — run BEFORE python-docx (or any zip consumer)
opens an untrusted upload (Resume Platform v3 spec section 4).

DOCX files are ZIP containers; a hostile archive can be a decompression bomb
(tiny file, enormous expansion) or hide nested containers. This module
inspects the central directory ONLY (no entry is extracted) and rejects:

  * more than ``MAX_ENTRIES`` entries;
  * total declared uncompressed size above ``MAX_TOTAL_UNCOMPRESSED``;
  * any entry whose compression ratio exceeds ``MAX_COMPRESSION_RATIO``;
  * nested container entries (.zip/.rar/.7z/... inside the archive).

Raises ``ZipGuardError`` (a ``ValueError``) with a user-safe message; the
upload routes translate that to a 400. Callers that only degrade (e.g. the
local text extractor) catch it and fall back.
"""
from __future__ import annotations

import io
import zipfile

MAX_ENTRIES = 2048
MAX_TOTAL_UNCOMPRESSED = 60 * 1024 * 1024  # 60 MB declared, summed
MAX_COMPRESSION_RATIO = 120  # per entry, uncompressed / compressed
# Entries below this compressed size are exempt from the ratio check —
# legitimate tiny XML parts (e.g. 40-byte rels) compress extremely well.
_RATIO_CHECK_MIN_COMPRESSED = 256

# Nested containers are never legitimate inside a resume DOCX.
_NESTED_CONTAINER_SUFFIXES = (
    ".zip", ".rar", ".7z", ".gz", ".tgz", ".tar", ".bz2", ".xz",
    ".jar", ".war", ".ear", ".docx", ".xlsx", ".pptx", ".docm",
)


class ZipGuardError(ValueError):
    """The archive violates a structural safety bound."""


def guard_zip_archive(file_bytes: bytes) -> None:
    """Validate an untrusted ZIP container (DOCX) against bomb heuristics.

    Args:
        file_bytes: Raw bytes of the uploaded archive.

    Raises:
        ZipGuardError: If the archive is malformed or violates any bound.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    except (zipfile.BadZipFile, Exception) as e:  # noqa: BLE001 — any parse failure is a reject
        raise ZipGuardError("File is not a valid DOCX archive.") from e

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ZipGuardError("DOCX archive has too many entries.")

        total_uncompressed = 0
        for info in infos:
            name = (info.filename or "").lower()
            if name.endswith(_NESTED_CONTAINER_SUFFIXES):
                raise ZipGuardError("DOCX archive contains a nested archive.")

            total_uncompressed += max(0, info.file_size)
            if total_uncompressed > MAX_TOTAL_UNCOMPRESSED:
                raise ZipGuardError("DOCX archive expands beyond the allowed size.")

            compressed = max(0, info.compress_size)
            if compressed >= _RATIO_CHECK_MIN_COMPRESSED:
                if info.file_size > compressed * MAX_COMPRESSION_RATIO:
                    raise ZipGuardError(
                        "DOCX archive entry exceeds the allowed compression ratio."
                    )
