#!/usr/bin/env python3
"""Check the public resume API through the edge, without credentials or writes.

This checks reachability and the layout-spec contract, not a deployed revision,
the other API services, or their database/LLM integrations. The former /api/health
check could accept the SPA's HTML fallback as success.
"""
from __future__ import annotations

import json
import math
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

URL = "https://aspirely.me/api/resume/layout-spec"
MAX_RESPONSE_BYTES = 64 * 1024


def validate_layout(body: bytes) -> None:
    """Require stable layout fields without pinning presentation values."""
    data = json.loads(body)
    if not isinstance(data, dict) or not isinstance(data.get("page"), str) or not data["page"].strip():
        raise ValueError("expected a layout-spec object with a page name")
    for key in ("page_width_mm", "page_height_mm"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"invalid layout dimension: {key}")
    sections, headings = data.get("section_order"), data.get("headings")
    if not isinstance(sections, list) or not sections or not all(isinstance(s, str) and s for s in sections):
        raise ValueError("expected nonempty section_order")
    if not isinstance(headings, dict) or not all(isinstance(headings.get(s), str) and headings[s] for s in sections):
        raise ValueError("expected a heading for each layout section")


def main() -> int:
    for attempt in range(1, 4):
        try:
            request = Request(URL, method="GET", headers={"User-Agent": "aspirely-deploy-check", "Accept": "application/json", "Cache-Control": "no-cache"})
            with urlopen(request, timeout=15) as response:
                if response.status != 200:
                    raise ValueError(f"expected HTTP 200, received {response.status}")
                content_type = response.headers.get_content_type()
                if content_type != "application/json":
                    raise ValueError(f"expected application/json, received {content_type}")
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("layout-spec response exceeds 64 KiB")
                validate_layout(body)
            print(f"Resume API layout-spec JSON verified through the edge: {URL}")
            return 0
        except (URLError, OSError, ValueError) as exc:
            print(f"resume API check attempt {attempt}/3 failed: {exc}", file=sys.stderr)
            if attempt < 3:
                time.sleep(5)
    print("Resume API verification failed; its edge response is not confirmed healthy.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
