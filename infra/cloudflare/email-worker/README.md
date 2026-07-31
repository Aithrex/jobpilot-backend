# JobPilot managed-mailbox inbound Email Worker

Thin Cloudflare Email Worker for the **managed application mailbox** inbound
path (`docs/specs/managed-mailbox-v1.md` §4). It runs on the Email Routing
catch-all for the neutral `APPLY_MAIL_DOMAIN`, and for every inbound message
performs the two-phase handoff to the backend:

```
Cloudflare catch-all
        │  email(message)
        ▼
  [size gate 25 MiB]
        │
   POST …/slot   (HMAC over  to\nfrom\nmessage-id\nsize)  ──► backend mints presigned S3 PUT
        │  {upload_url, put_headers, commit_token}
   PUT  raw MIME ─────────────────────────────────────────► S3 (presigned, one-use, no AWS creds)
        │
   POST …/commit (HMAC over  commit_token\nraw_sha256)     ──► backend parses/stores/correlates
```

The Worker does **no MIME parsing**, holds **no AWS credentials**, and never
logs secrets, bodies, or PII — only sizes, the message-id, and HTTP status.

- `src/worker.js` — the `email()` handler.
- `src/worker.test.js` — `node --test` unit tests for the protocol (no cloud). Run `npm test`.
- `wrangler.toml` — name, entrypoint, `MAIL_INBOUND_URL` var, secret note.

## Bindings

| Binding | Type | Value |
|---|---|---|
| `MAIL_INBOUND_URL` | plain var (`wrangler.toml`) | Base URL of the backend endpoints, e.g. `https://aspirely.me/api/resume/mail-inbound/cloudflare`. The Worker appends `/slot` and `/commit`. |
| `MAIL_INBOUND_HMAC_SECRET` | **secret** (`wrangler secret put`) | The SAME value seeded into backend SSM at `{ssm_path_prefix}/mail-inbound-hmac-secret`. Never commit it. |

## Local test

```bash
npm install      # dev-only: wrangler
npm test         # runs src/worker.test.js on Node's built-in runner
```

### Manual two-phase test against a local backend

With the backend running locally (`python local.py`, port 5000) and
`MAIL_INBOUND_HMAC_SECRET` set to a known value `S`:

```bash
BASE=http://localhost:5000/api/resume/mail-inbound/cloudflare
S='the-shared-secret'

TO='harshith.manne@pilotmail.co'
FROM='no-reply@greenhouse.io'
MID='<abc123@greenhouse.io>'
SIZE=42

# Phase A — slot. Signature = HMAC-SHA256 over to\nfrom\nmessage-id\nsize.
SLOT_SIG=$(printf '%s\n%s\n%s\n%s' "$TO" "$FROM" "$MID" "$SIZE" \
  | openssl dgst -sha256 -hmac "$S" -hex | sed 's/^.*= //')
curl -s -X POST "$BASE/slot" \
  -H "content-type: application/json" -H "x-jp-signature: $SLOT_SIG" \
  -d "{\"envelope_to\":\"$TO\",\"envelope_from\":\"$FROM\",\"message_id\":\"$MID\",\"raw_size\":$SIZE}"
# -> {"upload_url":"…","put_headers":{…},"commit_token":"…"}

# PUT the raw bytes to upload_url (from the slot response), then:
RAW_SHA=$(printf 'raw-bytes' | openssl dgst -sha256 -hex | sed 's/^.*= //')
TOKEN='<commit_token from slot>'

# Phase B — commit. Signature = HMAC-SHA256 over commit_token\nraw_sha256.
COMMIT_SIG=$(printf '%s\n%s' "$TOKEN" "$RAW_SHA" \
  | openssl dgst -sha256 -hmac "$S" -hex | sed 's/^.*= //')
curl -s -X POST "$BASE/commit" \
  -H "content-type: application/json" -H "x-jp-signature: $COMMIT_SIG" \
  -d "{\"commit_token\":\"$TOKEN\",\"raw_sha256\":\"$RAW_SHA\"}"
# -> {"ok":true,"thread_id":"…","message_id":"…","deduplicated":false}
```

## Deploy (exact steps — spec §12)

> Prereq: the neutral domain is registered and the backend S3 bucket + CMK exist
> (`infra/terraform/managed_mailbox.tf` applied) and `mail-inbound-hmac-secret`
> is in SSM. See `docs/ops/managed-mailbox-setup.md` for the full runbook.

1. **Add the domain to Cloudflare** (Add site → follow nameserver change).
2. **Enable Email Routing** for the domain (Email → Email Routing → Get started;
   Cloudflare adds the MX + SPF records automatically). Wait until MX is active.
3. **Set the backend URL** in `wrangler.toml` (`MAIL_INBOUND_URL`) if it differs
   from the default `https://aspirely.me/api/resume/mail-inbound/cloudflare`.
4. **Authenticate + deploy the Worker:**
   ```bash
   cd infra/cloudflare/email-worker
   npm install
   npx wrangler login                 # once, interactive
   npx wrangler secret put MAIL_INBOUND_HMAC_SECRET   # paste the SSM value
   npx wrangler deploy                # or: npm run deploy
   ```
5. **Create the catch-all route → this Worker:** Cloudflare dashboard → Email →
   Email Routing → Routing rules → **Catch-all address** → Action **Send to a
   Worker** → select `jobpilot-mail-inbound` → Save.
6. **Smoke test:** send an email to any address `@<domain>` and confirm a thread
   appears via the Inbox API. Tail logs with `npx wrangler tail` (breadcrumbs
   only — no PII). Then flip `APPLY_MAIL_DOMAIN` + `APPLY_MAILBOX_ENABLED` on the
   backend (spec §11).

## Notes

- Cloudflare Email Workers have a hard CPU cap on the free plan. This handler is
  intentionally allocation-light (one hash pass over the raw bytes); do not add
  MIME parsing here — parsing/sanitization is the backend's job (spec §4.2/§6).
- `message.to` is the **envelope** recipient (authoritative); the MIME `To`
  header is never trusted for alias resolution (spec §4.2, D7).
- The presigned PUT is the Worker's ONLY capability into AWS — one object, one
  use, short TTL. Rotating `MAIL_INBOUND_HMAC_SECRET` requires updating BOTH the
  Worker secret and the SSM value together.
