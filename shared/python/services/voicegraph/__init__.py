"""Voice Graph — knowledge graph + voice profile from a user's own writing.

Compass addendum §6.12, realized on JobPilot's MongoDB + Flask + Claude-on-Bedrock
stack (see docs/decisions/ADR-002-voicegraph-on-mongo-flask.md). Two artifacts are
kept strictly separate:

  * knowledge graph  — *what is true* about the person (governs which facts appear)
  * voice profile    — *how they say things* (governs how those facts are phrased)

Hard rules that live in code, not in a prompt:
  * every graph node/edge carries a verified evidence span (see store.py)
  * no objective optimizes for detector-evasion (docs/product/authentic-not-undetectable.md)
  * the writing sample is untrusted input to our own system (§9.4) — wrapped and
    parsed into a closed schema before anything escapes.

This package is import-safe: nothing here touches Mongo/SSM/Bedrock/Fernet at
import time (the CI smoke test does `import app; create_app()` with no AWS creds).
All side-effecting work happens inside functions, lazily.
"""

CONSENT_SCOPE = "voice_graph"
