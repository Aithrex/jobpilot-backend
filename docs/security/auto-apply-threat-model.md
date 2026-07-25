# STRIDE Threat Model — Auto-Apply + Managed Mailbox

Method: STRIDE per element across the two planes' trust boundaries (see
`auto-apply-dataflow.md`). Adversary classes: (a) anyone on the internet who can
send email to a guessable managed address or POST to the public edge routes;
(b) a hostile ATS page / recruiter email (untrusted content into a browser and
into the LLM); (c) a compromised/buggy Cloud Run worker; (d) a malicious job
posting that controls `apply_url`; (e) a user attempting IDOR against another
user. "Never 401 for auth failures" (invariant #1) holds on every machine route.

Each row: **Threat · STRIDE · Component · Current mitigation (code) · Residual /
recommended control.** Severity of the residual is tagged where a gap exists;
the actionable version lives in `auto-apply-audit.md`.

---

### T1 — Spoofed SES/Cloudflare inbound events
- **STRIDE:** Spoofing.
- **Component:** `mail_inbound.py` slot/commit; `worker.js`.
- **Mitigation:** Every inbound request is HMAC-SHA256 authenticated
  (`webhook_hmac.verify`, constant-time, empty-secret fail-closed). SES is no
  longer wired (Cloudflare-only). Auth failure → **403** (not 401).
- **Residual:** The HMAC secret is the single edge trust anchor; there is no
  timestamp/nonce so a captured signed request is replayable (idempotency makes
  replay harmless — see T-REPLAY). **Recommend:** add a signed timestamp to the
  canonical string and reject skew > N minutes; rotate `MAIL_INBOUND_HMAC_SECRET`
  procedure documented.

### T2 — Forged MIME headers (From/To/Authentication-Results)
- **STRIDE:** Spoofing / Tampering.
- **Component:** `mail_store_service._parse_mime`, `resolve_alias`.
- **Mitigation:** Owner is resolved from the **envelope** recipient
  (`resolve_alias(envelope_to)`), never the MIME `To`. SPF/DKIM/DMARC parsed as
  evidence only. Correlation (§8) requires the sender domain to match a
  *submitted* application before `received_at` for `verified` trust; forged
  headers cannot fake that. `from_domain` is compared in Python, never placed in
  a Mongo filter.
- **Residual:** DMARC is not an accept/reject gate by design; a spoofed sender
  still lands in the `unverified` bucket (visible, cannot auto-transition).
  **Recommend:** for `verified` upgrade, additionally require DKIM=pass or
  DMARC=pass on the correlating message (defense-in-depth on the OTP/status path).

### T3 — Forged worker callbacks (fake terminal / credential pulls)
- **STRIDE:** Spoofing / Elevation.
- **Component:** `executor.py`, `executor_capability_service.py`.
- **Mitigation:** `/claim` requires the one-use capability (atomic CAS); every
  other call requires the scoped `callback_token`; only digests are stored;
  owner identity is read from the bound token, never the body. Terminal is
  one-use (`mark_terminal_reported`). Failures → 403.
- **Residual:** A callback token leaked within its 15-min TTL can make one of
  each un-consumed call. One-use flags + presubmit re-check bound the blast
  radius. **Recommend:** bind callback calls to the worker's OIDC identity too
  (belt-and-suspenders), and shorten TTL to the p99 run time.

### T4 — Forged approval links (magic-link replay / cross-user approve)
- **STRIDE:** Spoofing / Elevation of privilege.
- **Component:** `application_ledger_service.approve`, forward deep links.
- **Mitigation:** `approve` folds in `user_email`, guards on
  `{status ∈ ready_for_review|ready}` via CAS, and is idempotent (a replayed
  click hits a no-op CAS miss — no re-approve, and with the reservation
  idempotency key, no re-charge). Forward deep links carry **no token**
  (`mail_forward_service`: `/next/applications/<thread_id>`) and land on an
  auth-gated page.
- **Residual:** The *email-approve* action route itself (defect J's magic link,
  in `blueprints/apply.py`) was out of this review's file scope — verify it is a
  non-mutating **GET** that renders a CSRF-protected **POST**, and that its token
  is single-use + owner-bound. **Recommend:** confirm/close in the audit
  (P1-APPROVE-GET).

### T5 — Alias spoofing / collision (two users, one address; recycled alias)
- **STRIDE:** Spoofing / Tampering.
- **Component:** `managed_mailbox_service` provision/rotate/`_mint_primary_alias`.
- **Mitigation:** Unique `local_part` index spans **all** states incl. `retired`
  tombstones (anti-recycle); collision resolves deterministically
  (`base`→`base2..N`→`base.<hex>`); `DuplicateKeyError` is the authority under
  concurrency. `resolve_alias` returns owners only for `active`/`paused`;
  `retired` → None (never reveals a prior owner). Local part is immutable for the
  mailbox life.
- **Residual:** None material. Rotation retires-then-mints; the old alias is
  reserved forever.

### T6 — Application / résumé / answer tampering (submit something the user didn't approve)
- **STRIDE:** Tampering.
- **Component:** approval snapshot + `presubmit_recheck`.
- **Mitigation:** Approval freezes an immutable snapshot
  (`application_hash` = recon fingerprint, `resume_version_id`,
  `answers_fingerprint`, `policy_version`, `consent_version`;
  `_build_approval_snapshot`). `presubmit_recheck` re-computes and compares
  (`_snapshot_stale`) immediately before the click; any drift → abort back to
  review, never submit stale. The worker fills only vetted `field_id→value`
  answers from `execution_envelope` (never invents values).
- **Residual:** Depends on the snapshot builder covering every integrity-relevant
  field; free-text answers are fingerprinted as a set. Acceptable.

### T7 — Duplicate or repudiated submissions
- **STRIDE:** Repudiation / Tampering.
- **Component:** ledger CAS + unique index; capability/terminal one-use; Cloud
  Tasks task-name dedupe.
- **Mitigation:** Unique `(user_email, job_identity)` makes a duplicate row
  structurally impossible; `dedup_gate` skips already-handled postings; the
  one-use capability claim stops a redelivered task before a second browser
  launches; `submission_unknown` is **held, never auto-retried**
  (`finalize_submission`). Append-only `auto_submit_audit` records every
  transition (non-repudiation).
- **Residual:** Audit log is append-only by convention (insert-only), not
  DB-enforced WORM. **Recommend:** document/enforce immutability (no update/delete
  IAM on the audit collection path).

### T8 — Cross-user mailbox / receipt / application exposure (IDOR)
- **STRIDE:** Information disclosure / Elevation.
- **Component:** every `mail_store_service` / `managed_mailbox_service` /
  `application_ledger_service` read; executor artifact issuance.
- **Mitigation:** **Every** Mongo filter folds in `user_email`; a foreign id is
  "not found" → **404** (never a 403 that confirms existence). Untrusted path/query
  ids are `InputSanitizer`-sanitized before the filter. Presigned résumé/receipt
  keys are derived server-side from the owner-bound token/row
  (`pinned_resume_s3_key`, `issue_receipt_put`), never from the request body.
  Attachment download 404s on foreign id, 403s on quarantined.
- **Residual:** None material in the reviewed paths.

### T9 — Malicious attachments + HTML/XSS in recruiter mail
- **STRIDE:** Tampering / Elevation (client-side).
- **Component:** `mail_store_service` `_scan_attachment`, `_inspect_zip`,
  `_sanitize_html`.
- **Mitigation:** Attachments validated by **magic bytes**, not extension;
  polyglot / encrypted / nested-archive / zip-bomb (expansion-ratio) → quarantined
  (`scan_verdict` suspicious/blocked, no downloadable key, download → 403). HTML
  sanitized server-side (drop script/style/iframe/form/svg/…; strip `on*` +
  `style`; neutralize `javascript:`/`data:`; remote images off) and the FE
  re-sanitizes in a sandboxed `srcdoc` iframe. Raw MIME never exposed.
- **Residual:** No live AV/virus engine (signature+structure heuristics only);
  a novel malicious-but-well-formed PDF passes as `clean`. **Recommend:** wire an
  AV scan verdict (spec §6 anticipates `scan_verdict`) before the flag flip if
  attachments are downloadable.

### T10 — Task / email floods (DoS + cost amplification)
- **STRIDE:** Denial of service.
- **Component:** inbound `/slot`+`/commit`; forward path; Cloud Tasks.
- **Mitigation:** Worker size-gates at 25 MiB before reading bytes; structural
  §6 limits fail-closed in `ingest_committed`; forwarding has a per-user daily cap
  (`_within_daily_cap`, atomic `$inc`); Cloud Tasks queue has global rate limits +
  bounded retries; auto-submit is per-user capped via the entitlement reservation;
  Cloud Run concurrency 1 + scale-to-zero + budget alert (`budget.tf`).
- **Residual (Medium):** **No per-user / per-domain rate limit on inbound
  slot/commit** — a flood of small messages to a known alias drives S3 PUTs +
  (async) LLM classification cost. **Recommend:** per-alias + per-source-domain
  ingest rate limit (atomic counter, same idiom as the forward cap). Also add a
  **per-domain / per-ATS** dispatch limiter (see T-RATE).

### T11 — Worker privilege escalation (worker → AWS/GCP control plane)
- **STRIDE:** Elevation of privilege.
- **Component:** `cloud_run.tf`, `service_accounts.tf`, `wif.tf`,
  `executor_capability_service`.
- **Mitigation:** Runtime SA holds only `logging.logWriter` (no Mongo/SSM/
  bucket-wide S3/general KMS). Everything is handed one-use + short-TTL via the
  callback surface. WIF pins the AWS account + exact role; enqueuer SA has only
  `workloadIdentityUser` + `actAs`; invoker SA can invoke only that one service.
  Cloud Run is internal-only, no `allUsers`.
- **Residual:** Strong. The remaining lever is what the worker's *browser* can
  reach on the network (T13/T14), not the GCP IAM surface.

### T12 — Malicious page / email prompt injection (into Claude and back into the FE)
- **STRIDE:** Tampering (integrity of AI output).
- **Component:** mail classifier (reused `mail_ingress_service.classify`), the
  fill-engine normalizer LLM fallback, FE render.
- **Mitigation:** In the fill/gather loop the model **only classifies** field
  text into the **closed** catalog (`normalize_field(allow_llm=False)` in the
  gather path; `field_id` from a closed set or `unknown`) — it never supplies an
  answer value; CODE fills. The mail classifier is a suggestion only behind an
  evidence + confidence gate and can never auto-transition state
  (`correlate` sets trust/suggestion only). Model output that renders in the FE
  goes through server-side HTML sanitization + a sandboxed iframe.
- **Residual:** Keep instructions in `system`, untrusted resume/JD/recruiter text
  in `user`; treat classifier output as untrusted (already schema-closed).
  IntegrityGuard is the content backstop. Acceptable.

### T13 — ATS-page SSRF + DNS rebinding (browser as a confused deputy)
- **STRIDE:** Information disclosure / Elevation.
- **Component:** `worker.py` `adapter.open(page, apply_url)`; `ats_adapters`.
- **Mitigation:** `apply_url` is stored via `create_application` (sanitized
  string) and the ATS family is detected; supported adapters are host-needle
  gated.
- **Residual (HIGH — P0):** `apply_url` is attacker-influenceable and the worker
  navigates it with a **full Playwright browser that has no request interception,
  no egress allow-list, and no scheme/host revalidation on redirects / subresources
  / popups / downloads / websockets**. A posting whose `apply_url` (or a redirect
  from it) points at an internal/link-local/loopback address, or that rebinds DNS
  mid-navigation, turns the worker into an SSRF deputy. **Recommend:** HTTPS host
  allow-list with suffix-boundary matching (the supported ATS families), block
  private/loopback/link-local/metadata/reserved IPv4+IPv6, deny
  `file:`/`data:`/custom schemes, Playwright request interception that
  re-validates every navigation/redirect/subresource, and a Cloud Run VPC egress
  policy.

### T14 — Cloud metadata-service access (169.254.169.254)
- **STRIDE:** Information disclosure / Elevation.
- **Component:** Cloud Run networking; `worker.py`.
- **Mitigation:** Runtime SA is near-powerless (log-write only), so a leaked
  runtime token is low-value.
- **Residual (HIGH — P0):** The GCP metadata endpoint is still reachable from the
  container; combined with T13 the browser could fetch it. **Recommend:** block
  `169.254.169.254` (and `metadata.google.internal`) at the egress layer and in
  the browser request-interceptor deny-list; set the metadata concealment /
  restrict the runtime SA further. Deliver via `infra/gcp/terraform` (VPC
  connector + egress firewall) — devops-engineer.

### T15 — Open-relay abuse (turn the forwarder into a spam cannon)
- **STRIDE:** Elevation / DoS / reputation.
- **Component:** `mail_forward_service.forward_notification`.
- **Mitigation:** Forwarding only ever sends a **sanitized notification** (no raw
  body, no attachments) from the JobPilot-owned `MAIL_FORWARD_FROM` to the user's
  **verified** `forward_to` — never to an arbitrary recipient and never spoofing
  the recruiter. Loop guards: `X-JP-Forwarded` marker, own-domain sender, self
  sender. Suppression list checked. Per-user daily cap.
- **Residual (Medium):** `forward_notification` does **not** gate on
  `trust == "verified"` — any inbound to a provisioned+forwarding-enabled alias
  produces a "message about your application" email whose **Subject/employer are
  attacker-controlled** (HTML-escaped, framed, no body). A guessable alias
  (`firstname.lastname@domain`) is thus a low-grade phishing-amplification vector
  into the user's real inbox. **Recommend:** gate the forward on
  `trust == "verified"` (or clearly label unverified/uncorrelated mail and drop
  the attacker-controlled subject), per D8's "unsolicited mail never auto-forwards
  with content" intent.

### T16 — Consent-revocation races (user pauses/revokes mid-flight)
- **STRIDE:** Tampering / Elevation (acting without current consent).
- **Component:** `presubmit_recheck` (`autopilot_active` check); mailbox `paused`.
- **Mitigation:** The pre-submit gate re-checks pause/revocation **and** the
  master kill-switch server-side immediately before the click; the worker aborts
  on `ok=false` **without** clicking. Mailbox `paused`/`forwarding_enabled` are
  re-read at forward time. Mode is a single source of truth
  (`auto_submit_settings.mode`, read-through in `managed_mailbox_service`).
- **Residual:** A revocation between the *precheck pass* and the *click* (a
  sub-second window) can still submit. Inherent to any external-side-effect
  system; the held-`submission_unknown` + audit trail bound it. Acceptable.

### T17 — Approval-snapshot staleness (approve now, form/résumé/answers change, submit later)
- **STRIDE:** Tampering.
- **Component:** `_snapshot_stale` + approval TTL in `presubmit_recheck`.
- **Mitigation:** The three integrity fields are re-validated before submit; drift
  → invalidate approval back to `ready_for_review` (which VOIDs the snapshot and,
  under G, releases the reservation). An approval older than
  `_approval_ttl_hours()` → `approval_expired`, blocked.
- **Residual:** None material; re-approval re-freezes a fresh snapshot and
  re-reserves against a fresh idempotency key.

### T18 — Entitlement double-charge (race / redelivery)
- **STRIDE:** Tampering (billing integrity).
- **Component:** `quota_ledger_service.reserve` + `application_ledger.approve`/
  `_settle_reservation`.
- **Mitigation:** Reservation is a single guarded `$inc` bounded below the cap
  (no check-then-increment), idempotency-keyed on `application_id + snapshot`, so
  concurrent/replayed approvals collapse onto one reservation. Settlement is
  idempotent (reserved→settled state guard): finalize on `confirmed`, release on
  safe terminals, hold on `submission_unknown`. Tailor keeps its pool-aware path
  (never double-counted).
- **Residual:** Correctness depends on `AUTO_APPLY_RESERVE` being ON in prod and
  every terminal flowing through `ledger.transition` (it does). Verify the flag is
  flipped before the execution plane is armed.

---

### T-REPLAY — Inbound HMAC replay (cross-cutting, referenced by T1/T2)
- **STRIDE:** Spoofing. **Mitigation:** `(alias_id, provider_message_id)` unique
  index + one-use commit token make a replayed slot/commit idempotent. **Residual
  (Low):** no timestamp/nonce; recommend adding one (see T1).

### T-CRED — ATS credential exposure at reveal (cross-cutting, referenced by T3)
- **STRIDE:** Information disclosure. **Mitigation:** one-use per execution
  (`credential_issued` flag), decrypted via KMS-envelope vault, returned once over
  TLS, held in worker memory only, dropped ASAP (`creds = None` after auth),
  never logged/disked. **Residual (Low):** plaintext transits the TLS callback
  once; acceptable given one-use + short TTL.

### T-RATE — Missing per-domain / per-ATS politeness limiter (cross-cutting, T10)
- **STRIDE:** DoS / reputation. **Mitigation:** global queue rate limits + Cloud
  Run concurrency 1 + per-user entitlement cap. **Residual (Medium):** no
  per-target-domain limiter; a burst of applications to one ATS tenant is only
  globally throttled. Recommend a per-domain/per-ATS token bucket at dispatch.
