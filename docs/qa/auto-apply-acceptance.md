# Auto-Apply + Managed-Mailbox — QA Acceptance Report

Status: **GREEN-GATED for a flag-off merge to main** (all features dark/flag-gated; nothing active in
prod until flags are flipped per `docs/ops/auto-apply-rollout-rollback.md`).

## Green state
- **Backend:** `PYTHONPATH=shared/python python -m pytest tests/ -q` → **1646 passed, 1 failed**. The
  single failure `tests/test_inventory_serving.py::test_lenient_mode_is_the_rollback` is **pre-existing**
  (fails standalone on the untouched baseline; a guest-service US-location lenient-mode assertion,
  unrelated to this feature).
- **Frontend:** `pnpm build` clean; `pnpm test` → 167 passed, 4 failed (all pre-existing
  `combined-jobs-titles.test.tsx`); `pnpm lint` 108 pre-existing `no-explicit-any`/style errors (none in
  touched files).

## Test isolation fix
Root cause: the suite shares one mongomock client; several fixtures build a unique index on
`applications` (`(user_email, job_identity)`), and mongomock's `delete_many({})` clears docs but not
indexes on the shared singleton. The backfill test's canonical rows (no `job_identity`) then collided.
Fixed in `tests/conftest.py` with an autouse `_reset_mongomock_indexes` that drops secondary indexes
before each test (all index-creating fixtures are function-scoped and rebuild their own). No assertion
weakened.

## Coverage matrix — ATS × capability
| Capability | Greenhouse | Lever | Ashby | Workday | Generic |
|---|---|---|---|---|---|
| detect | ✓ | ✓ | ✓ | ✓ (theme-independent) | ✓ (fallback) |
| recon | ✓ | ✓ (EEO) | ✓ | ✓ (multi-step) | ✓ |
| fill | ✓ | ✓ | ✓ | ✓ (automation-id) | ✓ |
| multi-step | n/a | n/a | n/a | ✓ | n/a |
| submit-boundary | ✓ | ✓ | ✓ | ✓ | review-only |
| confirmation | ✓ | ✓ | ✓ | ✓ | n/a |
| receipt | ✓ | ✓ | ✓ | ✓ | n/a |
| takeover (CAPTCHA/MFA/account/drift) | ✓ | ✓ | ✓ | ✓ | →human |

Evidence suites: `test_ats_adapters` (26), `test_workday_adapter` (20), `test_fill_driver` (5),
`test_auto_apply_edges` (7), `test_ats_executor` (33), `test_cloud_run_executor` (16).

## Safety evidence
- **Never-fabricate / sensitivity gate:** a required protected EEO / legal / work-auth field with no
  declared value → `waiting_human`/`needs_input`, field named, zero clicks
  (`test_auto_apply_edges`, `test_fill_driver`). An explicit "decline" IS a valid declared value and is
  filled. No wrong option is ever written (option-mismatch → `needs_input`).
- **SSRF:** `test_ssrf_guard` (47) + `test_jd_fetcher_ssrf` (39) — metadata/localhost/RFC1918/link-local/
  v4-mapped, scheme deny, DNS-rebinding pin, ATS suffix-boundary allow-list. Network backstop = Cloud Run
  egress firewall (P0-2).
- **Idempotency / atomicity:** atomic exactly-once entitlement reservation (`test_entitlement_reservation`,
  17); duplicate provider-message-id + forged envelope (`test_mail_inbound`); idempotent backfill
  (`test_reader_dualread`).
- **`submission_unknown`** held, clicked exactly once, never retried (`test_fill_driver`,
  `test_ats_adapters`, `test_workday_adapter`).
- **IDOR:** owner-scoped mailbox + application reads (`test_mailbox_api`, `test_auto_apply_edges`).

## Per-persona sign-off
Developer (unit), Tester (adversarial/edge), Delivery (integration recon→fill→submit→receipt and
dispatch→presubmit→finalize), End-user per mode (Copilot prepares-only; Approval/Autopilot submit behind
approval + presubmit re-check) — all green.

## Honest limitations
- **Workday:** account-gated multi-step wizard → frequently routes to human takeover (supported *with
  caveat*, not full autonomy).
- **Generic:** review-only; never autonomously submits.
- **LinkedIn:** never automated (policy).
- **Browser layer** tested via the offline lxml `FakePage` harness (live Chromium suite self-skips without
  Playwright); Bedrock is faked; Mongo is mongomock. A live staging round-trip is required before GA.
- **Not live:** everything is flag-gated/dark-launched.
