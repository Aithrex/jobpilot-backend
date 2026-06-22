"""Validate that Claude on AWS Bedrock is reachable with the current creds.

Run after adding AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / BEDROCK_REGION to
.env and enabling Anthropic model access in the Bedrock console:

    python scripts/check_bedrock.py

Exits 0 on success (prints the model's reply), non-zero with a diagnosis on
failure (bad creds, region without model access, throttling, etc.).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

import os


def main():
    region = os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION") or "us-east-1"
    has_key = bool(os.getenv("AWS_ACCESS_KEY_ID"))
    print(f"Region: {region} | AWS_ACCESS_KEY_ID set: {has_key}")
    if not has_key:
        print("Note: no AWS_ACCESS_KEY_ID in env — boto3 will fall back to ~/.aws or an IAM role.")

    try:
        from services.gemini_client import gemini_json, GEMINI_FLASH, GEMINI_PRO
        print(f"FLASH model: {GEMINI_FLASH}")
        print(f"PRO   model: {GEMINI_PRO}")
        print("Calling FLASH with a tiny prompt…")
        res = gemini_json(
            prompt='Return strict JSON: {"ok": true, "where": "bedrock"}',
            model=GEMINI_FLASH,
            max_tokens=64,
            temperature=0.0,
            max_retries=1,
            schema={"ok": bool, "where": str},
        )
        print("Reply:", res)
        if res and res.get("ok") is True:
            print("\n✅ Bedrock/Claude is working. AI features are live.")
            return 0
        print("\n⚠️  Got a response but not the expected shape — model access likely OK, check output above.")
        return 0
    except Exception as e:
        msg = str(e)
        print(f"\n❌ Bedrock call failed: {msg[:300]}")
        if "security token" in msg.lower() or "UnrecognizedClient" in msg:
            print("→ The AWS credentials are missing/invalid. Set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in .env.")
        elif "AccessDenied" in msg or "not authorized" in msg.lower():
            print("→ Creds work but lack bedrock:InvokeModel, OR model access isn't enabled in this region.")
        elif "could not be found" in msg.lower() or "ValidationException" in msg:
            print("→ The model ID isn't available in this region — enable Claude access in Bedrock → Model access, or change BEDROCK_REGION.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
