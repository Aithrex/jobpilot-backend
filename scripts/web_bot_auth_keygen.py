"""Make a Web Bot Auth signing key for the crawler (no database, no network).

Prints the private seed to put in the box's environment as WEB_BOT_AUTH_KEY
(both the API's stack env, which serves the key directory, and
/etc/jobpilot/ingest-runner.env, which signs requests), plus the public JWK and
its keyid for the Cloudflare Verified Bots form. Keep the seed secret; never
commit it. See shared/python/services/web_bot_auth.py.

    python scripts/web_bot_auth_keygen.py
"""
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared" / "python"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding, NoEncryption, PrivateFormat)

from services import web_bot_auth  # noqa: E402


def main() -> None:
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    jwk = web_bot_auth.public_jwk(key)
    print("WEB_BOT_AUTH_KEY=" + base64.b64encode(seed).decode())
    print("# public JWK (safe to share):", json.dumps(jwk))
    print("# keyid (JWK thumbprint):", web_bot_auth.thumbprint(jwk))
    print("# directory once deployed:", web_bot_auth.agent_origin() + web_bot_auth.DIRECTORY_PATH)


if __name__ == "__main__":
    main()
