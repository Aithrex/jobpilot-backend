"""Check (and optionally rotate) the transactional-email credentials.

Why this exists: `services/email_service.py` swallows send failures on purpose,
so a dead mailer can never break signup or password reset. The cost is that a
bad credential drops 100% of email with nothing visibly failing. That is exactly
what happened -- the stored ZeptoMail token went invalid (TM_4001 "Invalid API
Token") and every password-reset email silently vanished. This script is the
check that makes that state visible in one command.

    # Diagnose the current prod credentials (sends nothing):
    python scripts/check_email.py --profile aithrex

    # Same, plus one real test email:
    python scripts/check_email.py --profile aithrex --send you@example.com

    # Validate a NEW ZeptoMail token and store it in SSM only if it works:
    ZEPTOMAIL_TOKEN_NEW="Zoho-enczapikey ..." python scripts/check_email.py \
        --profile aithrex --rotate-zeptomail

The token is never printed, only its length and prefix.

ZeptoMail tokens are bound to the data centre their Zoho org lives in, so the
probe tries every regional host. If exactly one accepts the credential, set
ZEPTOMAIL_API_URL to that host rather than replacing the token.
"""
import argparse
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

SSM_TOKEN_PARAM = "/jobpilot/prod/zeptomail-token"
FROM_ADDRESS = "no-reply@aspirely.me"

ZEPTO_HOSTS = (
    "api.zeptomail.com", "api.zeptomail.in", "api.zeptomail.eu",
    "api.zeptomail.com.au", "api.zeptomail.jp", "api.zeptomail.ca",
    "api.zeptomail.sa",
)


def _session(profile):
    import boto3
    return boto3.Session(profile_name=profile) if profile else boto3.Session()


def _auth_header(token: str) -> str:
    """ZeptoMail wants `Zoho-enczapikey <token>`; accept an already-prefixed
    value so a paste from the console works either way."""
    return token if token.lower().startswith("zoho-enczapikey") else f"Zoho-enczapikey {token}"


def probe_zeptomail(token: str, hosts=ZEPTO_HOSTS) -> dict:
    """Validate a token WITHOUT sending mail.

    Posts a payload with an empty recipient list. ZeptoMail authenticates before
    it validates the body, so an auth error means the credential is bad while a
    validation error means the credential is good. Returns {host: verdict}.
    """
    results = {}
    for host in hosts:
        try:
            r = requests.post(
                f"https://{host}/v1.1/email",
                json={"from": {"address": FROM_ADDRESS, "name": "Aspirely"},
                      "to": [], "subject": "probe", "htmlbody": "<p>probe</p>"},
                headers={"Authorization": _auth_header(token),
                         "Content-Type": "application/json",
                         "Accept": "application/json"},
                timeout=15,
            )
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            err = body.get("error", {}) or {}
            detail = (err.get("details") or [{}])[0]
            message = detail.get("message") or err.get("message") or "accepted"
            # Do NOT key off the HTTP status: ZeptoMail answers 401 for BOTH a
            # rejected credential and a rejected payload. Only the inner detail
            # code separates them -- SERR_157 / "Invalid API Token" is the auth
            # failure. Anything else (e.g. SERR_156 "Invalid email address",
            # which our deliberately empty recipient list provokes) means the
            # credential was accepted and only the body was refused.
            auth_failed = (
                detail.get("code") == "SERR_157"
                or "invalid api token" in message.lower()
            )
            results[host] = {
                "valid": not auth_failed,
                "status": r.status_code,
                "code": detail.get("code") or err.get("code") or "-",
                "message": message,
            }
        except Exception as e:
            results[host] = {"valid": False, "status": None, "code": "-",
                             "message": f"unreachable: {type(e).__name__}"}
    return results


def check_ses(session) -> dict:
    """Is SES able to send from our domain right now?"""
    out = {}
    try:
        sesv2 = session.client("sesv2", region_name="us-east-1")
        acct = sesv2.get_account()
        out["sending_enabled"] = acct.get("SendingEnabled")
        out["production_access"] = acct.get("ProductionAccessEnabled")
        out["max_24h"] = acct.get("SendQuota", {}).get("Max24HourSend")
    except Exception as e:
        out["account_error"] = str(e)
    try:
        ses = session.client("ses", region_name="us-east-1")
        domain = FROM_ADDRESS.split("@")[-1]
        attrs = ses.get_identity_verification_attributes(Identities=[domain])
        out["domain"] = domain
        out["verification"] = attrs["VerificationAttributes"].get(domain, {}).get("VerificationStatus", "NotFound")
        dkim = ses.get_identity_dkim_attributes(Identities=[domain])
        out["dkim"] = dkim["DkimAttributes"].get(domain, {}).get("DkimVerificationStatus", "NotFound")
    except Exception as e:
        out["identity_error"] = str(e)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", help="AWS profile to read SSM / SES with")
    ap.add_argument("--param", default=SSM_TOKEN_PARAM, help="SSM parameter holding the ZeptoMail token")
    ap.add_argument("--send", metavar="EMAIL", help="after checking, send one real test email to this address")
    ap.add_argument("--rotate-zeptomail", action="store_true",
                    help="validate $ZEPTOMAIL_TOKEN_NEW and write it to SSM only if it is accepted")
    args = ap.parse_args()

    session = _session(args.profile)
    failures = []

    # --- ZeptoMail ---------------------------------------------------------
    print("== ZeptoMail ==")
    token = os.getenv("ZEPTOMAIL_TOKEN_NEW") if args.rotate_zeptomail else None
    if args.rotate_zeptomail and not token:
        print("  ZEPTOMAIL_TOKEN_NEW is not set -- nothing to rotate.")
        return 2
    if not token:
        try:
            token = session.client("ssm", region_name="us-east-1").get_parameter(
                Name=args.param, WithDecryption=True)["Parameter"]["Value"]
        except Exception as e:
            print(f"  could not read {args.param}: {e}")
            token = None

    if token:
        print(f"  token: length={len(token)} prefixed={token.lower().startswith('zoho-enczapikey')}")
        results = probe_zeptomail(token)
        good = [h for h, r in results.items() if r["valid"]]
        for host, r in results.items():
            mark = "OK  " if r["valid"] else "FAIL"
            print(f"  {mark} {host:24} HTTP {r['status']}  {r['code']:10} {r['message'][:50]}")
        if good:
            print(f"  -> credential accepted by: {', '.join(good)}")
            if "api.zeptomail.com" not in good:
                print(f"  -> set ZEPTOMAIL_API_URL=https://{good[0]}/v1.1/email (non-US data centre)")
            if args.rotate_zeptomail:
                session.client("ssm", region_name="us-east-1").put_parameter(
                    Name=args.param, Value=token, Type="SecureString", Overwrite=True)
                print(f"  -> stored in {args.param}")
        else:
            print("  -> credential REJECTED everywhere. Mint a fresh 'Send Mail' token in the")
            print("     ZeptoMail console (Mail Agents > your agent > SMTP & API > Send Mail Token).")
            failures.append("zeptomail")
            if args.rotate_zeptomail:
                print("  -> NOT written to SSM.")
                return 1
    else:
        failures.append("zeptomail")

    # --- SES ---------------------------------------------------------------
    print("\n== AWS SES ==")
    ses = check_ses(session)
    for k, v in ses.items():
        print(f"  {k}: {v}")
    ses_ok = ses.get("verification") == "Success" and ses.get("sending_enabled") and ses.get("production_access")
    print(f"  -> SES can send from {ses.get('domain')}: {bool(ses_ok)}")
    if not ses_ok:
        if ses.get("verification") == "Success" and not ses.get("production_access"):
            # The distinction that matters: a sandboxed SES account sends fine,
            # but ONLY to verified identities. It is therefore not a usable
            # fallback for password resets to arbitrary user addresses.
            print("  -> SES is still in the SANDBOX: it can only deliver to verified")
            print("     identities, so it is NOT a general fallback for user email.")
            print("     Request production access to make EMAIL_FALLBACK_PROVIDER=ses real.")
        failures.append("ses")

    # --- Verdict -----------------------------------------------------------
    print("\n== Verdict ==")
    if len(failures) >= 2:
        print("  BOTH transports are unusable -- no transactional email is being delivered.")
        return 1
    if failures:
        print(f"  {failures[0]} is down, but the other transport can cover for it")
        print("  (EMAIL_FALLBACK_PROVIDER). Delivery continues; fix the broken one.")
    else:
        print("  Both transports are healthy.")

    # --- Optional live send ------------------------------------------------
    if args.send:
        print(f"\n== Live test send to {args.send} ==")
        os.environ.setdefault("EMAIL_FROM_ADDRESS", f"Aspirely <{FROM_ADDRESS}>")
        if token:
            os.environ.setdefault("ZEPTOMAIL_TOKEN", token)
        from services import email_service
        res = email_service.send_test(args.send)
        for k, v in res.items():
            print(f"  {k}: {v}")
        if not res.get("sent"):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
