"""Feature flag for the Voice Graph feature.

`VOICE_GRAPH_ENABLED` — default ON in development, OFF in production until the
human-panel ship gate in addendum §6.12.9 passes. When off, every Voice Graph
route returns 404 so the feature is invisible in prod (safe to keep committing to
`main`, which auto-deploys).
"""
import os

from utils.config import _get_config_value

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def voice_graph_enabled() -> bool:
    raw = (_get_config_value("VOICE_GRAPH_ENABLED", "") or "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    # Unset → default by environment: on in dev, off in prod.
    env = os.getenv("ENVIRONMENT", "development").strip().lower()
    return env == "development"
