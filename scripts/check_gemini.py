"""Validate that Google Gemini is reachable with the current credentials.

The Gemini twin of `check_bedrock.py`. Works for both backends:

    # AI Studio: GEMINI_API_KEY in .env
    # Vertex AI: GEMINI_BACKEND=vertex + VERTEX_CREDENTIALS_JSON in .env
    python scripts/check_gemini.py

Exits 0 on success (prints the model's reply on both tiers), non-zero with a
diagnosis on failure (missing credentials, bad key, API not enabled, missing
IAM role, quota/billing, wrong model ID). It sends two tiny prompts and touches
no database. For the real features (parse, JD analysis, tailoring, chat), run
`scripts/check_ai_features.py` after this passes.

This intentionally forces the Gemini provider regardless of what LLM_PROVIDER
is set to, so you can verify the Gemini path works BEFORE flipping the switch.
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

_DOTENV_LOADED = False
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    _DOTENV_LOADED = True
except ImportError:
    pass

# Force the Gemini provider for this script only. Without this, running the
# script while LLM_PROVIDER=claude would test Bedrock and report success on a
# path we're not trying to validate.
os.environ["LLM_PROVIDER"] = "gemini"


def main():
    print(f"Python: {sys.executable}")
    if not _DOTENV_LOADED:
        print(
            "❌ python-dotenv is NOT installed in this interpreter — the project "
            ".env was NOT loaded. Fix: pip install python-dotenv"
        )

    try:
        import google.genai  # noqa: F401
    except ImportError:
        print(
            "❌ google-genai is not installed in this interpreter.\n"
            "   Fix: pip install -r shared/python/requirements.txt"
        )
        return 1

    from services.llm_providers.gemini import GeminiProvider
    try:
        backend = GeminiProvider.backend()
    except RuntimeError as e:
        print(f"❌ {e}")
        return 1
    print(f"Backend: {backend}")
    if backend == "vertex":
        raw = os.getenv("VERTEX_CREDENTIALS_JSON", "")
        if raw.strip():
            try:
                info = GeminiProvider._service_account_info(raw)
            except RuntimeError as e:
                print(f"❌ {e}")
                return 1
            # Never print the key itself — the account and project only.
            print(f"Service account: {info.get('client_email')}")
            print(f"Key project:     {info.get('project_id')}")
        elif os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            print(f"Credentials file: {os.getenv('GOOGLE_APPLICATION_CREDENTIALS')}")
        else:
            print("⚠️  No VERTEX_CREDENTIALS_JSON — trying Application Default Credentials")
        print(f"VERTEX_PROJECT:  {os.getenv('VERTEX_PROJECT') or '(from the key)'}")
        print(f"VERTEX_LOCATION: {os.getenv('VERTEX_LOCATION') or 'global'}")
    else:
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            print(
                "❌ GEMINI_API_KEY is not set.\n"
                "   Get a key at https://aistudio.google.com/apikey and add it to .env,\n"
                "   or set GEMINI_BACKEND=vertex to use a Google Cloud project."
            )
            return 1
        # Never print the key itself — just enough to tell two keys apart.
        print(f"GEMINI_API_KEY set: yes (…{api_key[-4:]}, len {len(api_key)})")
    print("LLM_PROVIDER: gemini (forced by this script)")

    try:
        from services.claude_client import claude_json, CLAUDE_FLASH, CLAUDE_PRO
        print(f"FLASH model: {CLAUDE_FLASH}")
        print(f"PRO   model: {CLAUDE_PRO}")

        # Both tiers are exercised because the pipeline needs both: FLASH for
        # extraction/verification, PRO for tailoring/JD analysis. Quota and
        # model availability are granted per model, so one working tier does
        # not imply the other does.
        ok = True
        for label, model in (("FLASH", CLAUDE_FLASH), ("PRO", CLAUDE_PRO)):
            print(f"Calling {label} with a tiny prompt…")
            res = claude_json(
                prompt='Return strict JSON: {"ok": true, "where": "gemini"}',
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

        # The system= path is what every real caller uses (guarded_system puts
        # trusted instructions out of the untrusted user message). The recovered
        # Gemini provider had no system support, so verify it explicitly here —
        # a TypeError on this call means the port is incomplete.
        print("Checking system-prompt support (used by every real caller)…")
        sys_res = claude_json(
            prompt="What is 2+2?",
            system='You always answer as strict JSON: {"answer": <number>}',
            model=CLAUDE_FLASH,
            max_tokens=64,
            temperature=0.0,
            max_retries=1,
        )
        print("system= reply:", sys_res)

        if ok:
            print("\n✅ Gemini is working on BOTH tiers. Next: python scripts/check_ai_features.py")
        else:
            print("\n⚠️  Got responses — the key works; check the shapes above.")
        return 0
    except TypeError as e:
        # Specifically the `system=` regression the port has to fix.
        print(f"\n❌ Provider signature mismatch: {str(e)[:300]}")
        print(
            "→ The Gemini provider's json() is missing a parameter the facade "
            "passes (most likely `system=`). Fix llm_providers/gemini.py."
        )
        return 1
    except Exception as e:
        msg = str(e)
        print(f"\n❌ Gemini call failed: {msg[:300]}")
        low = msg.lower()
        if "aiplatform.googleapis.com" in low or "service_disabled" in low or "has not been used" in low:
            print(
                "→ The Vertex AI API is not enabled in this project. Enable it:\n"
                "  https://console.cloud.google.com/apis/library/aiplatform.googleapis.com\n"
                "  (wait ~2 minutes after enabling, then re-run)."
            )
        elif "billing" in low:
            print("→ The project has no active billing account. Link one under Billing in the Cloud console.")
        elif "credentials" in low and ("not found" in low or "no google" in low or "default" in low):
            print("→ No credentials. Set VERTEX_CREDENTIALS_JSON to the base64 of the service-account key file.")
        elif "api key not valid" in low or "api_key_invalid" in low or "401" in msg:
            print("→ The API key is invalid or revoked. Regenerate it at https://aistudio.google.com/apikey")
        elif "429" in msg or "quota" in low or "resource_exhausted" in low:
            print(
                "→ Rate limited or out of quota. Free-tier Gemini Pro is ~5 RPM / 50 RPD;\n"
                "  if this is a new AI Studio account, Google requires Prepay billing to be\n"
                "  enabled before the API will serve requests. Check billing in AI Studio."
            )
        elif "permission" in low or "403" in msg:
            print(
                "→ The credentials are valid but not allowed to call the model.\n"
                "  Vertex: give the service account the role 'Vertex AI User'\n"
                "  (roles/aiplatform.user) in IAM, on the SAME project as VERTEX_PROJECT."
            )
        elif "not found" in low or "404" in msg:
            print(
                "→ The model ID doesn't exist or isn't served at this location.\n"
                "  Vertex serves current Gemini models at VERTEX_LOCATION=global (or us/eu).\n"
                "  Override with GEMINI_FLASH_MODEL_ID / GEMINI_PRO_MODEL_ID in .env."
            )
        elif "thinking" in low:
            print("→ The model rejected the thinking setting. Set GEMINI_THINKING_LEVEL=LOW (not MINIMAL).")
        return 1


if __name__ == "__main__":
    sys.exit(main())
