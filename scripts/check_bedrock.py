"""Validate that Claude on AWS Bedrock is reachable with the current creds.

Run after adding AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / BEDROCK_REGION to
.env and enabling Anthropic model access in the Bedrock console:

    python scripts/check_bedrock.py

Exits 0 on success (prints the model's reply), non-zero with a diagnosis on
failure (bad creds, region without model access, throttling, etc.).
"""
import sys
from pathlib import Path

# Windows consoles default to cp1252, which can't print the ✅/❌ markers.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

import os

# Diagnose the .env → environment handoff explicitly. load_dotenv does NOT
# override variables already set in the shell, so a stale AWS_PROFILE in the
# terminal silently wins over the project's .env — make that visible.
_PROFILE_BEFORE = os.getenv("AWS_PROFILE")
_DOTENV_LOADED = False
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    _DOTENV_LOADED = True
except ImportError:
    pass


def main():
    print(f"Python: {sys.executable}")
    if not _DOTENV_LOADED:
        print(
            "❌ python-dotenv is NOT installed in this interpreter — the project "
            ".env was NOT loaded. Fix: pip install python-dotenv"
        )
    profile_now = os.getenv("AWS_PROFILE")
    print(f".env loaded: {_DOTENV_LOADED} | AWS_PROFILE before .env: {_PROFILE_BEFORE!r} | in effect: {profile_now!r}")
    if _PROFILE_BEFORE and _DOTENV_LOADED:
        print(
            f"⚠️  Your SHELL already had AWS_PROFILE={_PROFILE_BEFORE!r} set — .env "
            "cannot override it. Clear it (PowerShell: Remove-Item Env:AWS_PROFILE) "
            "or open a fresh terminal."
        )

    region = os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION") or "us-east-1"
    has_key = bool(os.getenv("AWS_ACCESS_KEY_ID"))
    print(f"Region: {region} | AWS_ACCESS_KEY_ID set: {has_key}")
    if not has_key:
        print("Note: no AWS_ACCESS_KEY_ID in env — boto3 will fall back to ~/.aws or an IAM role.")

    # Show WHICH account/identity the calls will actually run as — a silent
    # fallback to the wrong ~/.aws default profile is the #1 local failure
    # (Bedrock model access is per-account; the JobPilot prod account has it,
    # an unrelated personal account does not).
    try:
        import boto3
        ident = boto3.client("sts", region_name=region).get_caller_identity()
        print(f"Caller identity: account {ident['Account']} — {ident['Arn']}")
    except Exception as e:
        print(f"Could not resolve caller identity (STS): {str(e)[:200]}")

    try:
        from services.claude_client import claude_json, CLAUDE_FLASH, CLAUDE_PRO
        print(f"FLASH model: {CLAUDE_FLASH}")
        print(f"PRO   model: {CLAUDE_PRO}")
        # Model access is granted PER MODEL — the tailor pipeline needs both
        # tiers (FLASH for extraction/verification, PRO for tailoring/JD).
        ok = True
        for label, model in (("FLASH", CLAUDE_FLASH), ("PRO", CLAUDE_PRO)):
            print(f"Calling {label} with a tiny prompt…")
            res = claude_json(
                prompt='Return strict JSON: {"ok": true, "where": "bedrock"}',
                model=model,
                max_tokens=64,
                temperature=0.0,
                max_retries=1,
                schema={"ok": bool, "where": str},
            )
            print(f"{label} reply:", res)
            if not (res and str(res.get("ok")).lower() == "true"):
                ok = False
                print(f"⚠️  {label} responded but not with the expected shape.")
        if ok:
            print("\n✅ Bedrock/Claude is working on BOTH tiers. AI features are live.")
        else:
            print("\n⚠️  Got responses — model access is OK; check the shapes above.")
        return 0
    except Exception as e:
        msg = str(e)
        print(f"\n❌ Bedrock call failed: {msg[:300]}")
        if "use case details" in msg.lower():
            print(
                "→ This AWS ACCOUNT has never been granted Anthropic model access.\n"
                "  Either (recommended) put keys from the JobPilot prod account into\n"
                "  .env (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY — prod already has\n"
                "  access), or enable it for THIS account: AWS console → Bedrock →\n"
                "  Model access → Modify → select Anthropic Claude models → submit\n"
                "  the use-case form → wait ~15 minutes → re-run this script.\n"
                "  Check the 'Caller identity' line above to see which account you hit."
            )
        elif "security token" in msg.lower() or "UnrecognizedClient" in msg:
            print("→ The AWS credentials are missing/invalid. Set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in .env.")
        elif "AccessDenied" in msg or "not authorized" in msg.lower():
            print("→ Creds work but lack bedrock:InvokeModel, OR model access isn't enabled in this region.")
        elif "could not be found" in msg.lower() or "ValidationException" in msg:
            print("→ The model ID isn't available in this region — enable Claude access in Bedrock → Model access, or change BEDROCK_REGION.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
