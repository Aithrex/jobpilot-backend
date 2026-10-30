"""Build the box's secrets file from the AWS backup. Prints names, never values.

    python infra/compose/render_stack_env.py /mnt/d/jobpilot-aws-backup OUTDIR

Reads secrets/ssm-prod.json and lambda/*.json, writes OUTDIR/stack.env and
OUTDIR/s3.env. Secrets it generates (the vault master key, the store's keys)
are kept in BACKUP/secrets/oracle-generated.json and reused on a re-run: losing
the master key loses every saved job-site password.
"""
import base64
import json
import secrets
import sys
from pathlib import Path

# Lambda-only, or pointing at AWS services this host does not use.
DROP_PREFIXES = ("SSM_", "AWS_", "_", "LAMBDA_", "BEDROCK_")
DROP = {
    "USE_SSM_SECRETS", "LD_LIBRARY_PATH", "PYTHONPATH", "TZ", "PATH", "LANG",
    "CREDENTIALS_KMS_KEY_ID", "APPLY_MAIL_KMS_KEY_ID", "SES_SNS_TOPIC_ARN",
    "APPLY_EXECUTOR_FUNCTION",
    "ASYNC_JOB_FUNCTION_NAME",
}
STORE = "http://s3:7070"


def lambda_settings(backup: Path) -> dict:
    merged: dict = {}
    conflicts = set()
    # Resume last: it is the function that ran the async jobs.
    names = sorted(p.name for p in (backup / "lambda").glob("*.json") if "apply-executor" not in p.name)
    names.sort(key=lambda n: "resume" in n)
    ssm_map: dict = {}
    for name in names:
        env = json.loads((backup / "lambda" / name).read_text()).get("Environment", {}).get("Variables", {})
        for key, value in env.items():
            if key.startswith("SSM_"):
                ssm_map[value.rsplit("/", 1)[-1]] = "JWT_SECRET_KEY" if key == "SSM_JWT_SECRET" else key[4:]
            elif not key.startswith(DROP_PREFIXES) and key not in DROP:
                if key in merged and merged[key] != value:
                    conflicts.add(key)
                merged[key] = value
    return merged, ssm_map, conflicts


def generated(backup: Path) -> dict:
    path = backup / "secrets" / "oracle-generated.json"
    if path.exists():
        return json.loads(path.read_text())
    values = {
        "CREDENTIALS_MASTER_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
        "S3_ACCESS_KEY": "jobpilot" + secrets.token_hex(8),
        "S3_SECRET_KEY": secrets.token_urlsafe(40),
    }
    path.touch(mode=0o600)
    path.write_text(json.dumps(values, indent=1))
    return values


def write(path: Path, values: dict) -> None:
    for key, value in values.items():
        if "\n" in str(value):
            raise SystemExit(f"{key} contains a newline")
    path.touch(mode=0o600)
    path.write_text("".join(f"{k}={v}\n" for k, v in sorted(values.items())))


def main() -> None:
    backup, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    env, ssm_map, conflicts = lambda_settings(backup)

    missing = []
    for row in json.loads((backup / "secrets" / "ssm-prod.json").read_text()):
        name = row["name"].rsplit("/", 1)[-1]
        if name in ssm_map:
            env[ssm_map[name]] = row["value"]
        else:
            missing.append(name)

    gen = generated(backup)
    env.update({
        "ENVIRONMENT": "prod",
        "USE_SSM_SECRETS": "false",
        "LLM_PROVIDER": "gemini",
        "CREDENTIALS_MASTER_KEY": gen["CREDENTIALS_MASTER_KEY"],
        "EMAIL_PROVIDER": "zeptomail",
        "EMAIL_FALLBACK_PROVIDER": "none",
        "AWS_ACCESS_KEY_ID": gen["S3_ACCESS_KEY"],
        "AWS_SECRET_ACCESS_KEY": gen["S3_SECRET_KEY"],
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ENDPOINT_URL_S3": STORE,
        "AWS_CONFIG_FILE": "/app/infra/compose/aws-config",
        "S3_PUBLIC_ENDPOINT_URL": "https://files.aspirely.me",
        "TRUSTED_PROXY_HOPS": "1",
        "ALLOWED_ORIGINS": "https://aspirely.me,https://www.aspirely.me",
        "EXECUTOR_REMOTE_ENABLED": "false",
        "WORKDAY_OVERALL_TIMEOUT": "210",
        "WORKDAY_MAX_WORKERS": "32",
        "PYTHONPATH": "/app/shared/python",
    })
    write(out / "stack.env", env)
    write(out / "s3.env", {
        "ROOT_ACCESS_KEY_ID": gen["S3_ACCESS_KEY"],
        "ROOT_SECRET_ACCESS_KEY": gen["S3_SECRET_KEY"],
    })
    print(f"stack.env: {len(env)} settings")
    print("settings:", " ".join(sorted(env)))
    if conflicts:
        print("differed between Lambdas (resume's value kept):", " ".join(sorted(conflicts)))
    if missing:
        print("secrets no Lambda referenced, not copied:", " ".join(sorted(missing)))


if __name__ == "__main__":
    main()
