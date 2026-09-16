# Verification gate

The offline backend suite is the required check. It must pass for the
commit being deployed. Local hooks are supplementary; GitHub Actions is
authoritative.

## Commands

Run from the **backend repo root**. Python **3.12**, no production
credentials, no paid model calls.

| When | Command |
| --- | --- |
| Fast, change-focused (renderer / pagination / selection) | `python3 scripts/verify.py --fast` |
| Fast, your selectors | `python3 scripts/verify.py --fast -- tests/test_foo.py::test_name` |
| Full pre-push / CI-equivalent | `python3 scripts/verify.py --full` |

**Invoke it as `python3`, not `python`.** Several dev environments here
(WSL, and any box without the `python-is-python3` shim) have no `python` on
PATH at all, so `python scripts/verify.py --full` never starts the suite. It
fails with `No such file or directory` and exit 127 — and wrapped in a
pipeline or a `timeout ... | tail` the 127 is easy to lose, which reads as a
pass that ran nothing. `verify.py` itself dispatches pytest through
`sys.executable`, so once it starts under `python3` everything downstream is
correct. `python3` is also right on CI, so there is one spelling everywhere.

`--full` is exactly `.github/workflows/tests.yml`:

```
ENVIRONMENT=development USE_SSM_SECRETS=false
PYTHONPATH=shared/python pytest tests evals -q
```

Install the same deps CI installs (do not upgrade them arbitrarily):

```
python -m pip install --upgrade pip
pip install -r shared/python/requirements.txt -r requirements-dev.txt
```

`requirements-dev.txt` includes `tzdata` so Windows `zoneinfo` can resolve
`America/Los_Angeles` the way Ubuntu CI does via system tzdata.

### Frontend (separate repo)

From the frontend root, Node 22, pnpm from `packageManager`:

| When | Command |
| --- | --- |
| Fast | `node scripts/verify.mjs --fast` |
| Full | `node scripts/verify.mjs --full` |

`--full` is `pnpm install --frozen-lockfile`, `pnpm test`, `pnpm copy:check`,
`pnpm build`.

## Pre-push hook (opt-in, repository-local)

Does **not** change global Git settings.

```
# backend
python scripts/install_pre_push_hook.py

# frontend
node scripts/install-pre-push-hook.mjs
```

The hook runs the full command with `--cache`. A stamp is reused only when
HEAD, the working tree, requirements/lockfile, and runtime version are
unchanged. Otherwise it reruns. `git push --no-verify` bypasses the hook;
CI still blocks a red suite.

## Linux / container path (renderer)

Bundled Charis SIL lives in `shared/python/services/fonts`. Local Windows
and CI should match when those files load. If a page-count test disagrees
with GitHub Actions, run the Ubuntu image CI uses:

```
bash scripts/verify-linux.sh
```

Requires Docker. Image default: `python:3.12.14-bookworm`.

## Deployment

`deploy.yml` waits for the `tests` workflow **for this commit SHA**. A
failed, cancelled, or missing suite refuses deploy. `workflow_dispatch`
is no longer an automatic bypass. The post-deploy edge health curl is
advisory (`continue-on-error`) and is not a substitute for the suite.

Branch-protection / rulesets that mark `tests` as required must be set
in the GitHub repository settings (admin). This private repo's rulesets
API currently returns 403 (GitHub Pro required) and `main` branch
protection returned 404 (not configured or not visible). Until an admin
marks the `tests` check required, PRs can still be merged while red;
`deploy.yml` will still refuse to ship a SHA whose suite failed.

## What this does not promise

A green local run does not make future failures impossible. The goal is
to catch reproducible failures before a commit is declared ready or
deployed, and to keep failed builds from shipping.
