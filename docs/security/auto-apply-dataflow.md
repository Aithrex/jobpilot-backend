# Data-Flow & Trust Boundaries — Auto-Apply + Managed Mailbox

Two planes, each crossing several trust boundaries. Every **capability / token /
presigned URL** is annotated where it is *minted* (M) and *consumed* (C). Trust
boundaries are drawn as subgraph borders; data crossing a border is fully
untrusted on the far side.

Legend: `M#` = minted here · `C#` = consumed here · dashed edge = one-use / short-TTL
capability.

---

## Plane 1 — Mail ingress (recruiter email → durable inbox → user's real inbox)

```mermaid
flowchart TD
  subgraph EXT[Untrusted Internet]
    REC[Recruiter / ATS mail server<br/>SPF/DKIM/DMARC = evidence only]
  end

  subgraph CF[Trust boundary: Cloudflare Email Routing]
    CATCH[Catch-all MX]
    WORKER["Email Worker worker.js<br/>holds ONLY the HMAC secret<br/>NO AWS creds, NO MIME parse"]
  end

  subgraph AGW[Trust boundary: AWS API Gateway edge]
    ROUTE["ANY /api/resume/{proxy+}"]
  end

  subgraph LAM[Trust boundary: resume Lambda]
    SLOT["/mail-inbound/cloudflare/slot<br/>HMAC verify (403 on fail)"]
    COMMIT["/mail-inbound/cloudflare/commit<br/>HMAC verify + sha256 verify"]
    RESOLVE[managed_mailbox_service.resolve_alias<br/>envelope-to authoritative, NoSQL-safe]
    INGEST[mail_store_service.ingest_committed<br/>parse+limits+sanitize+scan+correlate]
    FWD[mail_forward_service.forward_notification<br/>sanitized notify, loop/relay/rate guards]
  end

  subgraph S3[Trust boundary: S3 SSE-KMS APPLY_MAIL_BUCKET]
    RAW[(mail/raw/*.eml quarantine)]
    TXT[(mail/text, mail/html sanitized)]
    ATT[(mail/att validated files)]
  end

  subgraph DB[Trust boundary: Mongo aspirely_db]
    ALIAS[(mail_aliases owner-scoped)]
    THR[(mail_threads / mail_messages / mail_attachments)]
    TOK[(mail_commit_tokens TTL one-use)]
  end

  subgraph ZM[Trust boundary: ZeptoMail outbound]
    RELAY[MAIL_FORWARD_FROM verified sender]
  end

  subgraph USER[User's real mailbox]
    INBOX[forward_to verified inbox]
  end

  REC --> CATCH --> WORKER
  WORKER -- "POST slot + X-JP-Signature (M1 HMAC)" --> ROUTE --> SLOT
  SLOT --> RESOLVE --> ALIAS
  SLOT -- "M2 commit_token" --> TOK
  SLOT -- "M3 one-use presigned PUT (C1 HMAC)" --> WORKER
  WORKER -. "PUT raw MIME (C3 presigned)" .-> RAW
  WORKER -- "POST commit + sha256 (M4 HMAC)" --> ROUTE --> COMMIT
  COMMIT -- "C2 claim token (atomic one-use)" --> TOK
  COMMIT -. "GET raw (Lambda IAM)" .-> RAW
  COMMIT --> INGEST
  INGEST --> TXT
  INGEST --> ATT
  INGEST --> THR
  INGEST --> FWD
  FWD --> RELAY --> INBOX

  subgraph UI[Trust boundary: authenticated SPA]
    MB["/api/resume/mailbox/* JWT-required<br/>owner = get_jwt_identity()"]
  end
  MB -. "M5 short-TTL presigned GET (safe_html / attachment)" .-> TXT
  MB --> THR
  INBOX -. "auth-gated deep link /next/applications/<thread_id> (no token in link)" .-> MB
```

**Boundary notes (mail).**
- **M1/M4 HMAC** is the *only* edge auth (`webhook_hmac.verify`, constant-time,
  empty-secret fail-closed). SPF/DKIM/DMARC are stored as evidence, never a gate.
- **M2 commit_token** binds the presigned PUT to the resolved owner + message id
  in `mail_commit_tokens` (TTL 15 min, one-use CAS `claim_commit_token`).
- **M3 presigned PUT** is single-object (`raw_key_for`), single-use; the Worker
  holds no AWS creds — the URL is its entire capability.
- **M5 presigned GET** (5-min TTL) is the only way a client touches S3 bodies;
  raw MIME (`raw_s3_key`) is **never** exposed in any API field.
- Owner identity is always the JWT identity (user API) or the envelope-resolved
  alias owner (ingest) — **never** a body/MIME identity (D7 / IDOR defense).

---

## Plane 2 — Auto-apply execution (approval → dispatch → remote browser → ledger)

```mermaid
flowchart TD
  subgraph UI2[Trust boundary: authenticated SPA / email magic-link]
    APPROVE[user approves ready_for_review row]
  end

  subgraph LAM2[Trust boundary: resume Lambda orchestrator + vault]
    LEDGER[application_ledger_service.approve<br/>freeze snapshot + reserve entitlement]
    DISPATCH[cloud_run_executor.submit<br/>mint capability + enqueue ONE task]
    CB["/api/resume/executor/* callback surface<br/>403 not 401; identity from token"]
    ENV[auto_apply_service.execution_envelope<br/>vetted field_id->value answers only]
    PRE[presubmit_recheck<br/>kill-switch/pause/snapshot/TTL/quota/posting]
    FINAL[finalize_submission -> ledger CAS]
  end

  subgraph WIF[Trust boundary: GCP WIF - no static key]
    FED[AWS role -> enqueuer SA<br/>pinned account + exact role]
  end

  subgraph GCT[Trust boundary: Cloud Tasks queue]
    QUEUE[apply_executor queue<br/>rate limits + retry + task-name dedupe]
  end

  subgraph CR[Trust boundary: PRIVATE Cloud Run worker]
    RUN["worker.py _run()<br/>runtime SA = log-write only<br/>NO Mongo/SSM/bucket-wide S3"]
    PW[Playwright chromium<br/>drives apply_url]
  end

  subgraph ATS[Untrusted: ATS site]
    FORM[Greenhouse / Lever / Ashby form]
  end

  subgraph VAULT[Trust boundary: KMS-envelope vault + S3]
    CRED[(ats_credential_service reveal)]
    RESUME[(pinned tailored resume PDF)]
    RECEIPT[(auto-apply-audit/<app>/<exec>/receipt.png)]
    CAP[(executor_capabilities TTL, digests only)]
  end

  APPROVE --> LEDGER --> DISPATCH
  DISPATCH -- "M6 capability (bound execution)" --> CAP
  DISPATCH -- enqueue via --> FED --> QUEUE
  QUEUE -- "task body {execution_id, capability} + OIDC" --> RUN
  RUN -- "POST /claim {capability} (C6 atomic CAS)" --> CB
  CB -- "M7 callback_token (scoped)" --> RUN
  CB --> ENV
  RUN -- "POST /resume-url (C7)" --> CB
  CB -. "M8 one-use presigned GET (pinned key)" .-> RESUME
  RUN -. download resume .-> RESUME
  RUN -- "POST /credential (C7)" --> CB
  CB -. "M9 one-use plaintext cred (in-mem only)" .-> CRED
  RUN --> PW --> FORM
  RUN -- "POST /precheck (C7) LAST gate" --> CB --> PRE
  PRE -- ok=false -> ABORT (no click) --> RUN
  PW -- "click submit (irreversible)" --> FORM
  RUN -- "POST /receipt-url (C7)" --> CB
  CB -. "M10 one-use presigned PUT (audit prefix)" .-> RECEIPT
  RUN -. upload screenshot .-> RECEIPT
  RUN -- "POST /terminal {status} (C7) one-use flag" --> CB --> FINAL --> LEDGER
```

**Boundary notes (execution).**
- **M6 capability** rides the task body — the worker's only starting secret;
  claimed exactly once (`claim_capability` CAS) → double-dispatch backstop
  *before* any browser launches. Task name = `execution_id` also de-dupes at the
  queue layer.
- **M7 callback_token** authenticates every follow-up; only digests are stored
  (`executor_capability_service._digest`); each resource issuance is
  independently one-use (`_mark_issued`).
- **M8/M10 presigned URLs** are scoped to the ONE pinned résumé key
  (`pinned_resume_s3_key`, owner-scoped) and the ONE receipt key under
  `auto-apply-audit/<app>/<exec>/` — never bucket-wide.
- **M9 credential** is decrypted just-in-time via the KMS-envelope vault,
  returned once over TLS, held in worker memory only, never logged/disked.
- The **precheck** is the re-validation choke point (kill-switch, pause,
  approval snapshot drift/TTL, entitlement-held, posting) evaluated **server-side**
  immediately before the irreversible click; `submission_unknown` after the click
  is **held, never auto-retried**.

**The one border with no control today:** the `PW → FORM` edge. `apply_url` is
attacker-influenceable (it originates from a job posting recorded on the ledger
row) and the worker navigates it with a full browser that has **no request
interception, no egress allow-list, and no metadata-server block** — see the
audit (P0-1/P0-2). Everything else on this plane is one-use, owner-scoped, and
short-TTL; this edge is the exception.
