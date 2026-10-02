# JobPilot Backend

Flask API for the **Aspirely** AI career platform (`jobpilot` is the repo/internal name) — MongoDB + **Claude-on-Bedrock**, deployed to **AWS Lambda**. The frontend is a **separate repo** (`jobpilot-frontend`).

**5 deployed Lambdas** under `services/<svc>/`: four HTTP services (**auth · jobs · resume · billing**), each with its own `app.py` (Flask factory), `lambda_handler.py` and `blueprints/`; plus **`apply_executor`**, a headless container-image Lambda with **no HTTP routes** (invoked Lambda-to-Lambda). Shared business logic + framework live in the **`shared/python` Lambda layer** (mounted `/opt/python`). `from services.X` / `from blueprints.X` resolve across a service's local dir AND the shared layer via **PEP 420 namespace packages** (so NO `__init__.py` in any `services/`/`blueprints/` dir — adding one breaks the merge). `local.py` stitches the four HTTP services into one Flask app for local dev.

> **`services/learning/` is a NOT-YET-DEPLOYED skeleton.** It is absent from `var.service_config` in `infra/terraform/variables.tf`, and both `lambda.tf` and `apigateway.tf` `for_each` over that map — so no learning Lambda and no `/api/learning` edge route exist. `local.py` doesn't register it either. **Live traffic is served by `resume` at `/api/resume/learning/*`**; the skeleton exists to make the domain one-route extractable later (see its `app.py` docstring). Deploying it = add a `learning` entry to `service_config` + the deploy workflow's path filter.

## Commands (run from this directory)
- `python local.py` — local gateway: runs the four HTTP services as ONE Flask app on port 5000. Health: `GET /api/health`. **Needs MongoDB + AWS creds / Bedrock access** (without AWS creds, DB/search/career-pages work but every AI action fails).
- `python scripts/verify.py --fast` — change-focused offline checks (renderer/pagination/selection by default). Pass extra pytest selectors after `--`.
- `python scripts/verify.py --full` — **the offline test + eval suite** (`PYTHONPATH=shared/python pytest tests evals -q`). Fully deterministic: **mongomock** for Mongo, a **FakeLLM** for Bedrock — no AWS creds, no secrets, no network. This is exactly what `.github/workflows/tests.yml` and the optional pre-push hook run. See [`docs/qa/verification.md`](docs/qa/verification.md).
- `python scripts/install_pre_push_hook.py` — repository-local pre-push hook (does not change global Git config). CI remains authoritative.
- `pip install -r requirements-dev.txt` — pytest + mongomock + openpyxl (dev/CI only; **not** installed by the runtime requirements, so a fresh venv has no pytest).
- `pip install -r shared/python/requirements.txt` — layer deps (Flask/pymongo/boto3/google-genai/fpdf2/docx/pypdf/crypto/PyJWT/lxml/requests…). **All runtime deps live here** — there are no per-service requirements files.
- `python scripts/check_bedrock.py` — verify Bedrock creds + Claude model access (run before first use).

## Hard invariants (breaking these breaks login, billing, AI integrity, or prod)
1. **Auth error contract:** auth FAILURES return **400/409/403/429 — NEVER 401** (401 = `@jwt_required()` missing/bad token only; the FE force-logs-out on **any** 401). Auth success payloads are **flat**; reset uses **`new_password`**. JWT identity = email; **`JWT_SECRET_KEY` must match the legacy portfolio value** — don't rotate.
2. **Long work = async job, never inline:** `get_resume_service().create_job(type,payload,user_email)` + `ResumeService.invoke_async(...)` → `{job_id}`; add an `elif job_type==…` branch in `shared/python/services/resume_service.py::_process_job`; client polls `GET /api/resume/job/<id>` (state in `resume_jobs`). Dispatch = Lambda self-invocation.
3. **Gate before you spend:** `entitlements_service.require_feature(key)` (→403 `upgrade_required`) / `check_and_increment(email,feature)` **before** any LLM call. Tailor quota = `usage_service`/`tailor_usage` (don't double-count).
4. **Sanitize all untrusted input** with `shared/python/utils/security.py::InputSanitizer` before **any** Mongo filter (NoSQL injection). Never place user input in an operator position. Use atomic `$inc` for counters (concurrent Lambdas).
5. **AI only via the provider abstraction:** `services/claude_client.py` is the facade **all** services call (`claude_json`, `CLAUDE_FLASH/PRO/PREVIEW`) — never call boto3 Bedrock (or google-genai) directly from routes/services. Under it, `services/llm_providers/` selects at runtime on **`LLM_PROVIDER`** (SSM in prod, env locally): **`claude`** = Claude on AWS Bedrock via boto3 `invoke_model`, `anthropic_version: bedrock-2023-05-31`, **no Anthropic SDK** (default) · **`gemini`** = Google Gemini via `google-genai`. The public surface is provider-agnostic, so switching touches no call site. Tiers: **FLASH = Haiku 4.5** (`us.anthropic.claude-haiku-4-5-20251001-v1:0`, ~8k out) for extraction; **PRO = Sonnet 4.6** (`us.anthropic.claude-sonnet-4-6`, 16k+) for tailoring/JD/scoring. **Never fabricate résumé facts** — `integrity_guard.py` + `fabrication_filter.py` enforce this; never weaken them. LLM output is schema-validated (`schemas/resume_schemas.py`) + repaired (`llm_providers/_json_repair.py`, max 6 rounds/25s).
6. **New `/api/<prefix>`** needs its prefix added to the owning service's `routes` list in **`var.service_config`** (`infra/terraform/variables.tf`) — `apigateway.tf` and `lambda.tf` both `for_each` over that map, registering `ANY <prefix>` + `ANY <prefix>/{proxy+}`. Not there ⇒ 404 in prod (this is exactly why `learning` is dark).
7. **Only `apply_executor` runs a real browser.** Never add Playwright/Chromium to another service — the others are zip Lambdas without the runtime.

## Stack
Flask (per-service factory `services/<svc>/app.py::create_app()`) · MongoDB `aspirely_db` via **raw PyMongo** (NO ORM, NO migration framework, **NO indexes defined in-repo → most queries are COLLSCAN**) · Claude on AWS Bedrock (or Gemini) · AWS Lambda (5× zip + 1× container `lambda_handler.py` + apig-wsgi) / local `local.py` gateway · PyJWT + flask-jwt-extended · bcrypt (12 rounds) · boto3 (S3/SES/SSM/Bedrock/Lambda) · Playwright + Chromium (`apply_executor` only) · fpdf2/python-docx/pypdf · cryptography. Tests: pytest + mongomock + FakeLLM.

## Architecture (thin blueprints → fat shared services → shared layer)
Each `services/<svc>/` is its own deployable: `app.py` (factory, registers only its blueprints + CORS/JWT/security-header hook), `lambda_handler.py` (apig-wsgi; **only `resume` has the async-job self-invoke branch**), `blueprints/`, and optionally a local `services/` of domain-only logic. `package.sh` bundles only `services/<svc>/` code — **a module a Lambda needs but doesn't own must live in the shared layer.**

| Lambda | Blueprints | Routes | Edge |
|---|---|---|---|
| **auth** | `auth` (JWT mint, OAuth, reset), `admin` (`@admin_required`) | ~40 | `ANY /api/auth/{proxy+}`, `ANY /api/admin/{proxy+}` |
| **jobs** | `jobs`, `apply` (extension) | ~58 | `ANY /api/jobs/{proxy+}`, `ANY /api/apply/{proxy+}` |
| **resume** | `resume`, `executor`, `guest`, `mailbox`, `mail_inbound`, `sprint_hub`, `voice_graph`, `tech_chronicle`, `feedback` | **~205** | `ANY /api/resume/{proxy+}`, `ANY /api/tech-chronicle/{proxy+}` |
| **billing** | `billing` (entitlements surface) | ~6 | `ANY /api/billing/{proxy+}` |
| **apply_executor** | — none — | 0 | not routed; invoked by `resume` |
| *(learning)* | `learning` | ~9 | **not deployed** — see the note above |

Every `resume` blueprint except `tech_chronicle` mounts under **`/api/resume/*`** (`feedback` → `/api/resume/feedback`, `voice_graph` → `/api/resume/voice-graph`, `sprint_hub` → `/api/resume/hub`, `guest` → `/api/resume/guest`, `mailbox` → `/api/resume/mailbox`, `mail_inbound` → `/api/resume/mail-inbound`, `executor` → `/api/resume/executor`) — which is why the service needs only two edge prefixes. Note `local.py` registers a **subset**: `mailbox`, `mail_inbound` and `executor` are prod-only, so those routes are absent from the local gateway.

**`resume` is the hub** (~205 of ~318 routes): tailoring, ATS, copilot, interviews, learning, voice, managed mailbox, guest flow — plus 12 local services (career-copilot, interview-coach, overview, career-readiness, job-intelligence, beta-features, recommendations, streak, github_*, linkedin_*).

**`apply_executor`** ([services/apply_executor/lambda_handler.py](services/apply_executor/lambda_handler.py)) is the auto-apply browser fleet: a Playwright/Chromium container image (~2 GB, 300 s, small reserved concurrency as a cost + politeness cap). `resume` reaches it via `LambdaInvokeExecutor` with `{"method","args"}` → dispatched to `shared/python/services/browser_executor.py::BrowserExecutor` (`recon_form`, `create_account`, `submit`, `verify_state`, `is_available`). It swallows exceptions into clean contract shapes — a bad call reads as unavailable/failed, never a stack trace.

**`shared/python/`** = the Lambda layer, shared by all 6: **~126 services** (incl. `resume_service` async orchestrator + its parser/tailor/scorer/renderer tree, `job_service`, `browser_executor`, `entitlements_service`, `usage_service`, `s3_service`, `gmail_service`, `claude_client`, `llm_providers/`, the job-source catalogs), `schemas/resume_schemas.py` (LLM output: PARSED/TAILORED_RESUME, JD_ANALYSIS, ATS_SCORES), `utils/` (`db_connect` Mongo singleton, `config` +SSM, `security`), `models/user.py` (pw rules: 8+ chars, upper/lower/digit/special, bcrypt), `requirements.txt`.

> **ONE match score:** `shared/python/services/fit_score.py` (ADR-002, `fit-v2.1`) is the **only** candidate-to-job match score. `/api/jobs/search`, `/api/jobs/pipeline/match-scores`, the inventory feed, the career-portal scans and the `apply_match_sweep` gate (band "good match" = **65**) all read it. Never add a second formula and never post-process its output under the same `match_score` key — carry ordering/preference signals in `rank_score` and tailorability in `tailor_feasibility`. **No parsed résumé ⇒ NO score** (`match_score = 0`, no `fit`/`score_version` marker → the FE renders "Not scored"); never a number.

> **Coupling note:** `jobs.py` uses `resume_service` and `resume_service` uses `job_service` — both live in the shared layer precisely because the jobs↔resume domains are circularly coupled. Keep cross-domain services in `shared/python/services/`; a shared service must NOT import a domain-local one.

## Employer identity and career-source ownership (ADR-006, 2026-09-02)

Two NEW collections, both additive and both reversible by dropping them.
`companies`, `company_ats_endpoints` and `job_postings` are untouched, so the
crawler reads exactly the rows it read before.

- **`employers`** (`shared/python/services/employer_registry.py`) — ONE
  canonical `employer_id` (`emp_<12 hex>`) shared by `companies`, the curated
  seed, the three Python catalogs, `company_ats_endpoints` and `job_postings`.
  Derived deterministically, domain-first: `sha1("domain:"+registrable_domain)`
  > `sha1("curated:"+key)` > `sha1("name:"+name_key)`, with
  `identity_confidence` 0.95 / 0.80 / 0.45. **Never merge two employers on a
  normalized name** — `merge_decision` returns `merge` only on a shared
  verified domain, a curated alias, or an owner-verified `(vendor, tenant)`;
  everything else that merely looks like a match is `quarantine` and goes to
  `employer_merge_reviews`. `operating_status` (`active|renamed|acquired|
  merged|closed|product_or_brand`) is load-bearing: the resolver FOLLOWS
  `parent_employer_id`, so an acquisition never deletes an employer from the
  denominator. `IDENTITY_OVERRIDES` repairs the documented conflicts (Stripe,
  Notion, Ramp, Capital One, Intuit, JPMorgan, Boeing, Roche, AT&T, Abbott,
  NVIDIA, Salesforce/Slack, Alphabet/BMW).
- **`career_sources`** (`career_source_service.py` + `career_source_ownership.py`)
  — a nine-state machine (`candidate, ownership_verified, content_validated,
  validated, empty, blocked, moved, dead, manual_review`). **Only `validated`
  and `empty` count as coverage.** Ownership needs EITHER a link/redirect
  chain from an independently verified employer domain to the exact endpoint,
  OR two corroborating signals from two DIFFERENT classes
  (`employer_domain` / `ats_provider` / `curated_registry`).
  `SIGNAL_SLUG_SIMILARITY` and `SIGNAL_ENDPOINT_RESPONDS` are recorded and
  **never counted** — that is the whole fix.

> **Registration answers ONE of the two questions.** `upsert_source` had
> `state` in `_MUTABLE` and `$set` it unconditionally, so re-registering a
> board that was already `validated` rewrote it to `ownership_verified` — a
> write that PROVED something good removed the employer from coverage. One
> `verify_official_career_urls.py --apply --register` run moved Intel, Adobe,
> Accenture, Palantir, Splunk, DeepL, d-Matrix, Aikido and Lakera out of the
> covered set, and Tier-A coverage read 24/94 instead of 30/94. Registration
> now never overwrites a state that carries a CONTENT verdict (`validated`,
> `empty`, `content_validated`); everything else it learned still lands, and
> demotion stays where the evidence for it is — `record_failure`. The one
> state a registration DOES move is `content_validated` + newly proven
> ownership, which is exactly `validated`: the board was already read and held
> postings, and the only thing it lacked has just arrived.

> **A vendor's CDN is not an employer's board.** Fingerprints run over every
> url a careers page mentions, and every Phenom customer's page loads the same
> four `*.phenompeople.com` asset hosts — so Adobe, Cisco and Splunk each
> registered `phenom:assets`, `phenom:pp-cdn`, `phenom:content-us` and
> `phenom:cdn-prod-static` as owner-verified career sources. The fix is the
> URL, not the name: `endpoints_from_page` drops asset references by
> EXTENSION before fingerprinting, which has no false positives because no ATS
> serves a board at a `.js` path. A name test does have them — an earlier
> draft refused `greenhouse:css` (CloudKitchens, née City Storage Systems) and
> `greenhouse:manifest` (Manifest Cyber), both live boards.
> `AtsProvider.is_reserved_tenant` is what is left: nine tokens no company is
> named after, plus compound infrastructure like `pp-cdn`.
> Anything the shared list is too blunt for goes on the vendor, as
> `AtsProvider.reserved_tenants` — `assets` is allowed for Greenhouse and
> refused for Phenom, because `assets.phenompeople.com` is the host every
> Phenom customer's page loads. `scripts/purge_infrastructure_sources.py`
> removed the 222 already written and refuses to delete any row the crawler is
> reading postings from.

> **A 403 to our prober is not evidence a board is gone.** The content pass
> checks a `blocked`/`empty` verdict against `_boards_with_fresh_postings`
> (active postings by `(ats, tenant)` in the last 48h) and records a NOTE
> instead of a demotion when the inventory contradicts it. Without that guard
> one pass run beside two others turned 830 sources `blocked` in a single
> sweep and dropped validated coverage by a third — our own concurrency, not
> anything at the employer.

> **`company_ats_endpoints.status == "validated"` is NOT coverage.** It means
> the endpoint answered. It cannot tell a company's own board from a
> stranger's board with a similar slug, and it is the number that reported
> 43.0%. Coverage now comes from `career_sources`. Both are printed side by
> side by the report so the gap is visible.

### Canonical employer id ON THE POSTING (`canonical-employer-1.0.0`)

`shared/python/services/canonical_employer.py` is the join `job_postings` did
not have. A posting gets `canonical_employer_id` ONLY when the board it came
from carries owner-verified evidence naming exactly one employer:

  * `career_sources.ownership_verified` is the gate — a guessed slug that
    returns jobs never reaches it, and neither does a name that merely matches;
  * `parent_employer_id` is FOLLOWED first, so Slack and Salesforce on one
    board is one employer, not a conflict;
  * two employers that survive the parent walk are QUARANTINED to
    `employer_board_conflicts` — **except** when exactly one of them holds a
    verified-domain chain (`SUFFICIENT_SIGNALS`), which outranks two
    corroborating signals for the same reason `adjudicate_ownership` makes 4a
    sufficient alone. That basis is recorded as `domain_chain_precedence` and
    the board is STILL filed for review, because attribution and identity are
    two questions.

`employer_id` is never read, rewritten or deleted — it is the key three
production indexes use — so the field is additive and
`scripts/backfill_canonical_employer_id.py --apply --unstamp` is the rollback.
The ingest path stamps it in `job_inventory_service.upsert_posting` and
**clears it when a board becomes contested**: a stale wrong id is worse than
none.

Everything employer-shaped now PREFERS it — company filters, facets, dedupe
(`_canonical_variant_key_of`), employer diversity, `employer_scope=tech_only`
— falling back to `(ats, tenant)` and only then to a name
(`tech_feed_service.employer_key_of`). Legacy `company_ids` values (a name
slug or a bare tenant) are still accepted, deliberately and temporarily.

`GET /api/jobs/tech-feed/companies` is the autocomplete
(`employer_directory.py`): names in, canonical ids out, and **nothing from
`career_sources`** beyond a `career_source_verified` boolean — ownership
evidence names the endpoints we fetch and the domains we probe, which is
operational detail about our crawl and not information about the employer.

> **A partial index's filter must be PROVABLE from the query.** Both canonical
> posting indexes were declared `partialFilterExpression: {canonical_employer_id:
> {"$type": "string"}}`. They indexed the right rows and the planner never
> chose either one — a partial index serves only a query it can prove is a
> subset of its filter, and an equality on a string does not prove
> `$type: "string"`. Measured on live inventory: the employer posting-count
> aggregation ran **1,544 ms** on an unrelated fallback plan and **17 ms** once
> the same index was declared `$exists: true`, which an equality (and an `$in`
> of equalities) does prove. `scripts/explain_job_inventory.py` reported both
> shapes as a green `IXSCAN` throughout, because it asserted the STAGE and only
> PRINTED the index name; it now declares the expected index per shape and
> fails when a different one answers.

> **`canonical_employer_status` exists for the `$or`, not for the filter.** A
> company filter carrying BOTH spellings — an old saved search plus a fresh
> autocomplete pick — is a `$or`, Mongo subplans exactly ONE rooted `$or`, and
> the freshness window is already it. So every branch of the company `$or` has
> to be independently indexable, and a branch that does not carry `status`
> cannot use either `status`-prefixed index. The legacy branches always could,
> through `employer_id_status`; this is that index's canonical twin. Measured
> live: **7,660 ms / 36,964 documents examined -> 18 ms / 87.**

> **The autocomplete queries a PREFIX before it scans.** A case-insensitive
> contains regex cannot use an index, so every keystroke examined all 23,782
> employers. `employer_directory.search` now runs an anchored, case-sensitive
> prefix query on `name_key` first (22 keys examined) and falls back to the
> contains scan only when that did not fill the page. `name_key` ALONE, and
> that is what makes it exact rather than merely fast: it is the only field
> written through `employer_registry.name_key`, so it is the only one a
> case-sensitive anchor can be trusted against — `aliases` keep their original
> capitalisation. Every row the fast query returns scores `prefix = 0` in
> `_rank_key` and every row the scan could add scores 1, so a full page of
> them is already the answer.

Scripts, all **dry-run by default**:
- `scripts/backfill_employer_registry.py [--apply]` — builds `employers` +
  `career_sources` from all five registries.
- `scripts/register_ingested_boards.py [--apply] [--refresh]` — registers the
  boards the crawler is ALREADY reading. 1,437 `(ats, tenant)` boards carrying
  124,226 active postings had no `career_sources` row at all, so pass 2 had no
  board to walk to. The employer names a board's own postings carry are the
  PROVIDER naming its owner (`ats_provider`) when one name covers >= 90% of at
  least three postings; a board naming many employers gets no signal, which is
  how Jobgether and JPMorgan's shared Oracle instance are correctly refused.
  A curated catalog row naming the same endpoint is the second, independent
  class (`curated_registry`) — and only counts when the two AGREE.
  `--refresh` never writes a weaker verdict than a row already holds.
- `scripts/verify_career_sources.py [--apply] --mode
  endpoints|employers|content|backlink|both` — pass 1 asks the ATS who owns
  the board (cheap); pass 2 verifies the employer's domain and walks from it
  to the board (decisive, and the only path that reaches Workday/iCIMS/Oracle,
  which name no owner); pass 3 validates content; **pass 4 (`backlink`) is the
  cheap middle and the highest-yield pass on live data** — for sources holding
  exactly ONE confirmed evidence class, it derives the employer's domain FROM
  THE BOARD and verifies it names them. 742 sources sit in that state, and the
  `employer_domain` class they are missing was unobtainable for every
  suffix-host vendor until `AtsProvider.board_url` landed.

> **Run ONE network pass at a time.** Not politeness advice — a measured
> failure: one content pass beside two others turned 830 sources `blocked` in
> a single sweep and cut validated coverage by a third.

> **The content pass must ask the board's REAL address.** Pass 3 rebuilt each
> endpoint as `Endpoint(vendor, tenant)` and discarded the pod and site path
> the source had already recorded. For Greenhouse and Lever a tenant IS the
> address; for the three vendors carrying most enterprise inventory it is not
> — Adobe is `adobe.wd5.myworkdayjobs.com/external_experienced`, Accenture is
> `wd103`, Snap is `wd1.myworkdaysite.com/recruiting/snapchat/snap`. All three
> came back `unparseable_response`, which reads as "their board is broken" and
> meant "we asked the wrong URL"; hydrating the endpoint turned them
> `validated` with 738, 2,000 and 182 live postings. `hydrate_endpoint` reads
> `company_ats_endpoints` first and the source's own `canonical_endpoint`
> second — the second matters because a source the ownership walk discovered
> has no catalog row yet, and that is exactly this pass's population.

> **`scripts/repair_canonical_endpoints.py` un-freezes the same bug from the
> data.** `https://icims.com/<tenant>` was written onto 631 sources before
> `AtsProvider.board_url` existed, and everything downstream reads the stored
> field — including the demotions to `dead` and `blocked` that those fetches
> caused. It repairs 333 of them with NO network requests (endpoint catalog ->
> redirect chain -> an active posting's apply URL, in that order of authority)
> and leaves 298 alone, because guessing a hostname is what caused this.

> **Pass 3 spends its budget where the number can move.** `ownership_verified`
> + content = `validated`; `content_validated` + content = `content_validated`
> again, since what it lacks is ownership this pass cannot supply. There are
> 1,197 of the second and 79 of the first, so an unordered `.limit()` drew 396
> of 400 rows that could not move and shifted coverage by zero. The selection
> is now two queries, movable first.
- `scripts/employer_coverage_report.py [--markdown] [--persist]` — THE
  accountability artifact. Read-only apart from `--persist`, which stores the
  measurement in `coverage_snapshots` so the PRODUCT reads the same numbers
  this report prints (`tech_feed_service.coverage_block` serves them with
  `measured_at`, `age_hours` and `stale`). Generates
  `docs/coverage/tech-employer-coverage.md` stamped with the source revision,
  the inventory watermark and every version that produced it.
- `scripts/coverage_work_queue.py` — read-only. What is uncovered, WHY, and
  what fixing it buys. Four buckets in the order they are worth doing
  (`content_pending`, `endpoint_unproven`, `source_no_endpoint`, `no_source`),
  each weighted by the postings behind it, Tier-A first. "1,749 employers are
  uncovered" is not a work queue; 1,365 of them hold 219 postings between them.
- `scripts/backfill_canonical_employer_id.py [--apply] [--unstamp]` — stamps
  `canonical_employer_id`, quarantines every board it refuses, and reverses
  itself.
- `scripts/verify_official_career_urls.py [--apply] [--register]` — the script
  `official_career_registry`'s docstring has always named. `verified_at` was
  hard-coded `None` and nothing could ever have written another value; it is
  now READ from `official_career_url_checks`, one row per domain, written only
  from an actual fetch. `verified_at` moves on `verified` alone: a 403 is
  recorded as `blocked` with its own timestamp and does not clear an earlier
  verification.
- `scripts/repair_employer_metadata.py [--apply]` — drops `other` where a
  specific category survives, records `category_evidence` (what each source
  DECLARED, including the terms that resolved to `other`), derives categories
  from the registrant's filed SIC code, and splits geography into
  `headquarters_country` (documentary only), `hiring_countries` (observed) and
  `remote_eligible`. An employer hiring in Poland is not a Polish company.
- `scripts/purge_infrastructure_sources.py [--apply]` — removes
  `career_sources` and `company_ats_endpoints` rows whose tenant names a
  VENDOR'S OWN infrastructure. It never deletes a row the crawler is reading
  postings from, which is the check that saved `greenhouse:css`
  (CloudKitchens) and `greenhouse:manifest` (Manifest Cyber) from a first
  draft that judged the name instead of the URL.
- `scripts/prioritize_reviews.py [--apply]` — orders `employer_merge_reviews`
  and `employer_board_conflicts` by CONSEQUENCE (core-tech postings ×3, active
  postings, Tier-A membership, evidence already gathered) instead of by
  arrival. A queue of 1,443 rows sorted by `created_at` is a queue nobody
  finishes.

## H-1B sponsorship intel (`sponsorship-ingest-2.0.0`, 2026-09-16)

`sponsorship_signals` was EMPTY until 2026-09-16 — the feature shipped and was
never run, so every badge returned `None`. It now holds 43,561 rows built by
`scripts/ingest_sponsorship.py` from the USCIS H-1B Employer Data Hub (FY2026),
and `services/sponsorship_service.py` serves them to `/api/jobs/sponsorship`,
search, guest cards and the digests. See
[`docs/coverage/h1b-sponsorship-ingest.md`](docs/coverage/h1b-sponsorship-ingest.md).

- **The provenance comes from the ROW, never from a module constant.** The
  window, source label, disclaimer and vintage are read off `data_window` /
  `source_label`. They used to be `DATA_WINDOW_YEARS = 5` and a label naming
  DOL+USCIS, so one fiscal year of USCIS rendered as "in the last 5 years" from
  "DOL OFLC LCA Disclosure Data + USCIS" — five years of two datasets we did not
  have. `data_vintage` is the fiscal PERIOD ("FY2026"); when we imported is
  `data_imported_at`.
- **Volume saturation is PER YEAR.** The constant was calibrated on a five-year
  window; held fixed, a one-year file put "strong" at 204 approvals in one year
  and a band would move when we imported more history rather than when the
  employer changed. A five-year file still scores identically.
- **A miss and a zero are different facts.** No row = we never looked
  (`lookup_status` says `no_row`). `census_absent: true` = the Data Hub lists
  every employer that filed, we checked every distinctive token of the name, and
  they are not in it. Northrop Grumman: 833 active postings, zero petitions.
  454 such employers hold 12,478 of our active postings.
- **Rollup merges on evidence only** (`scripts/data/sponsorship_rollups.py`):
  curated exact-name whitelist, `employers` registry at confidence >= 0.80, or
  Tax-ID last-4 + first token + state. USCIS prints four EIN digits, so that
  field is never probative alone. The file contains a Delray Beach stone
  importer named "AMAZON USA ENTERPRISE …" and an IT staffing firm named "GSK
  SOLUTIONS" — refusals are recorded next to the merges, and the 2,639 near
  misses go to a review artifact, never to a guess.
- `scripts/sponsorship_gap_report.py` is read-only and joins that demand against
  `employers`/`career_sources`/`company_ats_endpoints`/`job_postings` →
  `docs/coverage/h1b-sponsor-gaps.{json,csv}`.

## Job sourcing
Jobs come from JSearch **plus direct employer ATS portals** — the differentiator. Catalogs + engines live in the shared layer: `workday_tenant_catalog.py` / `workday_jobs_service.py` / `workday_cxs_fetcher.py` (Workday CXS) · `career_pages_catalog.py` / `career_pages_service.py` (**`FETCHERS`** greenhouse/lever/ashby/smartrecruiters/workable/recruitee; **`ENTERPRISE_FETCHERS`** amazon/eightfold/phenom/orc/uber) · `mnc_catalog.py` / `mnc_jobs_service.py`. Routes in `services/jobs/blueprints/jobs.py` (`/workday/*`, `/career-pages/*`, `/mnc/*`). Adding companies = catalog rows, validated live via `scripts/discover_workday_tenants.py` / `scripts/discover_career_pages.py`; a new ATS = a `_fetch_<ats>()` registered in `FETCHERS`. **Apify is disabled** (reversible — every edit tagged `APIFY DISABLED (temporary)`).

## Occupation taxonomy and match scoring (`tech-tax-1.2.0`, `fit-v2.1`)

`shared/python/services/tech_taxonomy.py` is the **canonical occupation
ontology** — 14 families / 106 specialties — and it is now what ingestion,
résumé parsing, search AND `fit_score` all score against. `fit_score.
ROLE_FAMILIES` (32 coarse keys) survives only as the LEGACY BRIDGE for
`job_postings.role_family`, which three production indexes key on; nothing in
the scoring path reads it for role alignment any more.

- **`semiconductor_hardware` is CORE**, not adjacent. It was adjacent, which
  meant the default scope hid every ASIC, RTL, verification, firmware, analog,
  RF, EDA and photonics role unless the searcher ticked an extra box.
- **Role alignment is a five-rung ladder** (`role_alignment`): `exact_specialty
  > adjacent_specialty > same_family > adjacent_family > unrelated`, over a
  versioned, DECLARED adjacency matrix (`ADJACENCY_VERSION`, 184 pairs). The
  previous implementation's finest software distinction was the single family
  `software_engineering`, so for anyone in software this 25-point component was
  near-constant and the whole score distribution compressed.
- **`unknown` is a sixth answer and is off the ladder.** Not knowing costs
  what it always cost; it must never cost what being wrong costs.
- **Unknown is EXCLUDED, not priced.** `_normalized_score` averages only the
  components with evidence on BOTH sides. Every component used to pay a
  "neutral" fraction when it had nothing to say, and a neutral fraction is
  still a deduction — on live data that was a systematic ~12-point tax on
  every candidate for gaps in OUR parsing (96.1% of postings state no work
  mode, 43.3% no seniority).
- **`eligibility` is separate from `score`** and is not a second score:
  `eligible | ineligible | not_scored`, fired only on a KNOWN mismatch — a
  confident cross-domain verdict, a named licence the résumé lacks, or a
  stated sponsorship conflict. Silence is never a mismatch; an unstated
  clearance is `unknown` and surfaces as a requirement to read, not a penalty.
- **`cross_domain` means a change of OCCUPATIONAL DOMAIN, never an unfamiliar
  technology family.** It is the only thing that turns role alignment into an
  INELIGIBLE verdict, and ineligible rows leave the ranked feed entirely. Two
  technology families the adjacency matrix does not connect score `unrelated`
  (0.08 — already a severe penalty) and stay eligible. Setting it on any
  unrelated tech pair made a software-and-cloud résumé ineligible for 522 of
  2,500 live core-tech postings, 478 of them security or staff/architect roles
  (`docs/coverage/fit-score-v21-distribution.md`).
- **`tech-adjacency-1.1.0`** adds the edges leadership and security are
  actually staffed from: IC specialties → `principal_staff_ic` /
  `architecture`, software → `application_security` / `product_security`,
  cloud → `iam_pam` / `cloud_security` / `secops`. `engineering_management`
  keeps no direct IC pair on purpose — it is reachable at the adjacent-family
  rung through the bridge, which is the honest distance.
- **Bumping `FIT_SCORE_VERSION` is safe now**, via
  `_COMPATIBLE_REQUIREMENT_VERSIONS`: the extraction contract is unchanged, so
  216,687 stored `requirements` profiles stay valid through the deploy.
- **`principal` is its own seniority band**, between `staff` and `director`.
  The feed accepted it as a filter and `job_postings.seniority` could never
  hold it.

### What `tech-tax-1.2.0` changed, and the evidence for each

Every one of these was measured on live `job_postings` first, and every one is
covered by `evals/eval_live_titles.py` (559 reviewed titles) or
`evals/eval_fit_score_domains.py` (115 labelled scenarios).

- **Four QUANTUM specialties**, on 71 distinct live titles / 77 postings:
  `quantum_software` (software family — a compiler engineer is a software
  engineer), `quantum_hardware`, `quantum_control` and `cryogenic_systems`
  (semiconductor family), plus 17 declared adjacency edges. Before them,
  "Quantum Software Engineer (Compiler)" read `general_software`, "Senior
  Cryogenic Systems Engineer" read cloud `infrastructure`, and "Sr. Quantum 3D
  Integration and Packaging Engineer" read **non_tech**.
- **An O*NET title alone is no longer sufficient.** It admitted "Field Service
  Engineer" (81 live postings) to `core` on the title alone, along with
  "Distribution Engineer", "Transmission Engineer" and "Intelligence Analyst".
  The fix is POSITIVE EVIDENCE, not a blocklist: `_TITLE_TECH_TOKEN_RE` asks
  whether the title itself names a technical domain, so "IT Business Analyst"
  keeps its reading and "Field Service Engineer" needs the posting to say
  something technical — which "…, Semiconductor Capital Equipment" does and
  "…, Elevators" never will.
- **`_DOMAIN_QUALIFIERS`** generalises the physical-engineering veto: a
  declared qualifier vetoes a technical role noun for the FAMILIES it covers.
  Eight live titles were `core` at high confidence and are not: Space Systems
  Engineer, GNC Analyst, an air-cooled chiller Product Engineer, a hotel's
  Assistant Director of Engineering, an FP&A analyst, a Corporate Counsel for
  Product Security, an adjunct faculty post and Energetics Manufacturing.
- **Hardware context now beats the physical veto**, and `_PHYSICAL_TO_
  SEMICONDUCTOR` maps the role noun into the fab specialty it becomes. A bare
  "Process Engineer" optimising a polymerization line stays non-technical.
- **`_norm` folds diacritics** instead of deleting them. "Préposé à
  l'entretien ménager" arrived as "pr pos e l entretien m nager", a string no
  rule can match; live inventory carries French, German, Spanish and
  Portuguese titles and every one was being shredded before any rule saw it.
- **~130 non-technical phrases** added from the live sample, which moved 114
  reviewed titles from `unknown` to `non_tech`. `unknown` and `non_tech` are
  both outside the default search, so this changes no retrieval — but
  `fit_score._eligibility` fires a confident cross-domain verdict only on
  `non_tech`, and `employer_tech_classifier`'s posting mix needs a
  non-technical denominator to place a private employer.

> **`fit_score`'s SKILL LEXICON had zero coverage of five core families.**
> Not one word of semiconductor, embedded, robotics, gaming or quantum
> vocabulary — no Verilog, RTL, UVM, RTOS, ROS, SLAM, Unreal, Qiskit. A senior
> ASIC designer's résumé yielded no readable skills at all, tripped
> `insufficient_resume_evidence`, and scored **20 against every job including
> their own discipline**. `semiconductor_hardware` is a CORE family; the
> default search returns those jobs and the scorer could not read the people
> who do them. Fixing it moved P@5 from 0.581 to 0.993 on
> `eval_fit_score_domains.py`. `_alias_lookup` was also silently discarding
> any alias written with `\b` — it matched, then failed the reverse lookup —
> which is now repaired and commented.

> **`_EXPERIENCE_RELEVANCE`: experience means RELEVANT experience.** The
> component measured calendar time, so seven years of backend scored full
> marks against a senior semiconductor requisition. Measured over 115
> scenarios, grade-3 pairs averaged 19.91/20 and grade-0 pairs 16.29 — 3.6
> points of separation. Scaling the component by role-alignment tier
> (`unknown` = 1.00, so not knowing still costs nothing) took separation to
> **7.95** with ranking unchanged. **No COMPONENT_MAX weight was changed.**

Backfills: `scripts/backfill_tech_taxonomy.py --apply` (version-aware — it
selects rows whose `tech.taxonomy_version` is not current, so a bump is
backfillable without `--all`) and `scripts/backfill_posting_normalization.py
[--apply] [--with-bodies]`, which brings stored postings up to
`NORMALIZATION_VERSION` from fields already on the document. The second one is
how 11,653 postings that stated "minimum N years" but read `seniority:
unknown` got their band back — retrieval filters on the stored value, so those
rows were invisible to anyone filtering by level. **It stamps the version only
when JD bodies were actually read** (`--with-bodies`, from our own S3):
`job_ingest_worker._listing_implies_same_content` treats the stamp as "needs
no re-fetch", so stamping a half-upgraded row would freeze it there.

Evals — all offline, deterministic and checked in:

| runner | what it measures |
| --- | --- |
| `evals/eval_tech_taxonomy.py` | 143 hand-written rows, per-family AND per-stratum precision/recall. Now 1.000/1.000 with stratum H at 44/44. |
| `evals/eval_live_titles.py` | **559 live titles, each reviewed by a human**, weighted the way the market is (334 non-technical). Reports scope accuracy, POSTING-weighted accuracy, non-tech leakage and — separately — accuracy on the 153 rows where the reviewer DISAGREED with the classifier. Those are the known defects; a change that improves the headline while regressing them has traded a measured bug for an unmeasured one. |
| `evals/eval_fit_score_domains.py` | **115 labelled scenarios across 14 domains** (backend, frontend, full stack, mobile, DevOps/SRE, cloud, security, data, AI/ML, embedded, semiconductor, robotics, gaming, quantum) over synthetic anonymised résumés. Eligibility accuracy split into false-INELIGIBLE (held at zero — it removes a reachable job) and false-eligible; P@5 and NDCG@5 per résumé; calibration (mean score must be monotone in the human grade); and EVERY component's grade-3-vs-grade-0 separation with its `known` rate, so a component that is flat because it is never known is distinguishable from one that is known and useless. |
| `evals/test_expanded_evals.py` | the CI gates for the two above. |
| `evals/eval_tech_feed_retrieval.py` + `evals/test_tech_feed_retrieval.py` | retrieval by source / family / specialty / cohort / ambiguous title / geography / seniority, plus the traversal-ceiling regression. |

## The tech-feed API surface added on 2026-09-02

All under the EXISTING `/api/jobs/tech-feed` subpath, so no new top-level
prefix and no Terraform edge route (invariant 6).

| route | what it is |
|---|---|
| `GET /tech-feed/companies?q=` | company autocomplete over the canonical registry. `?ids=a,b` resolves ids back to names and follows an acquisition. **Nothing from `career_sources`** beyond a `career_source_verified` boolean |
| `GET /tech-feed/company-categories` | the canonical employer-category facet with counts. `other` is reported LAST and labelled, not hidden |
| `GET/POST/DELETE /tech-feed/saved-searches` + `POST .../reconcile` | server-owned saved searches, keyed on the CLIENT's own id so a retried write is one row. Reconcile pushes up and NEVER deletes |
| `POST /tech-feed/telemetry` | non-PII product events. Salted one-way account pseudonym, closed dimension allowlist, filter KEYS only, bucketed counts. Returns 204 on a dropped batch — telemetry must never fail a user action |

New collections, all additive and all droppable without touching the crawl:
`employer_board_conflicts`, `coverage_snapshots`, `official_career_url_checks`,
`tech_feed_saved_searches`, `tech_feed_events` (TTL 90 days).

> **ONE set of coverage numbers.** `scripts/employer_coverage_report.py
> --persist` writes `coverage_snapshots`; `tech_feed_service.coverage_block`
> reads it and serves `measured_at`, `age_hours` and `stale` alongside every
> figure. The banner used to show a DIFFERENT, cheaper set of numbers because
> the real ones take two minutes to compute, and the two disagreed — one of
> them said 43%. `available: false` is a real answer and renders as "unknown",
> which is not zero.

## Retrieval: no 2,000-result ceiling

`tech_feed_service` pages in two phases. `_serve_page` replays the frozen,
personalised RANKED order (bounded by `MAX_ORDERED`, because it lives in a
session document). When that is exhausted the cursor CHANGES SHAPE to a
keyset — `{"s": id, "k": {"p": posted_at, "j": job_id}}` — and
`_serve_keyset_page` continues in `(posted_at DESC, job_id ASC)` index order
over every matching posting. Ordering stays total and stable, dedup still
runs, cursor expiry still 410s, and the response says `sort: "index_order"`
so a client can tell it is no longer relevance-ranked.

`total` is now the real match count (bounded by `COUNT_LIMIT`), not the length
of the ranked window.

**Employer diversity is applied to the ORDER, never to the score.**
`_diversify_employers` caps how many postings one employer may hold in a
single page (`EMPLOYER_PAGE_CAP`, default 3), pushing the rest to later pages.
Nothing is dropped and `match_score` is untouched — it belongs with
`rank_score`, where preference signals live. The key is **`(ats, tenant)`**,
the same key `employer_identity.apply_resolution` adjudicates on, because a
posting's `employer_id` is a slug derived from whatever name the board
printed: JPMorgan's Oracle instance serves three CX sites under tenant `jpmc`
and stamps its postings `jpmorgan-chase`, `nutmeg-container-corporation` and
`chase-family-limited-partnership-no-5`. Keyed on the slug, one employer took
nine of the first twenty-five cards. The cap is best-effort — a page is topped
up rather than shortened when the remaining results genuinely are one
employer — and it applies only to the first `EMPLOYER_DIVERSITY_DEPTH` rows
(250, ten pages). Diversifying the whole list would push a dominant employer's
later postings out of the `MAX_ORDERED` window into date order: JPMorgan holds
839 core-tech postings and a cap of 3 per 25-row page admits only ~240 within
2,000.

## Where the code actually runs (it is not all Lambda)

**Five Lambdas and one Oracle Cloud box.** The four HTTP services and
`apply_executor` deploy to Lambda; `services/ingest_runner` — THE CRAWL — is a
systemd unit on an **Oracle Cloud Always Free** A1 VM (`jobpilot-ingest`,
4 OCPU / 24 GB, us-ashburn-1), cut over from EC2 on 2026-10-02 to take ~$35/month
to $0. It moved off Lambda on 2026-09-02 because of the 300s wall: a shard could
not finish its members before being starved. **Stay at 4 OCPU / 24 GB** — only
that much A1 is free, and the tenancy quota allows more, which would bill.

> **Oracle deploys are PULLED** (`infra/oracle/`, runbook in its README). CI
> moves `deploy/ingest-runner` to a SHA whose suite is green; a 2-minute timer
> on the box installs it through the SAME `infra/ec2/install-ingest-runner.sh`
> and unit file. The deploy job is green only when `ingest_runner_heartbeat`
> shows that SHA from hostname `jobpilot-ingest` — the host filter exists
> because the first version passed on the EC2 heartbeat while Oracle could
> not fetch. The EC2 box (`i-06bf0e03e60d9d409`) was TERMINATED 2026-10-02 and
> its CI job deleted; the EC2 paragraphs below are history. `infra/ec2/` stays
> because its installer and unit file are what the Oracle box runs.

> **Reading `.github/workflows/deploy.yml` alone tells you the wrong thing.**
> Until 2026-09-02 that workflow knew only about Lambda, so the crawler was
> shipped by hand and drifted: the box was found running a build 20 hours
> older than `main`, and because `job_inventory_service.upsert_posting` is
> where `canonical_employer_id` is stamped and that module had never reached
> the box, **733 postings an hour were arriving unstamped** while the backfill
> reported 100% attribution. Nothing errored. The `deploy-ingest-runner` job
> now ships it on every push that touches `shared/python/**`,
> `services/ingest_runner/**`, `infra/ec2/**` or `scripts/**` — the same four
> trees the tarball contains.
>
> **That sentence said "three trees" and was false for three weeks.** The
> tarball held only `shared/python` and `services/ingest_runner`; the installer
> replaced the systemd unit `if [ -f ]`, which never was. So the box kept the
> cutover unit — `--no-maintenance` hard-coded — from 2026-09-02 to
> 2026-09-24, retention was owned by nothing, and the 05:45 coverage job
> (which imports `scripts/employer_coverage_report.py`) failed
> `ModuleNotFoundError` every morning. Every deploy was green. The installer
> now REQUIRES the unit and import-checks the coverage module, and the runner
> records each daily job in `ingest_runner_daily_runs` and its
> `maintenance` flag in the heartbeat, which `retention-watchdog` checks.

EC2 deployment was over **SSM, not SSH**: no inbound port, no key material in CI.
`infra/ec2/install-ingest-runner.sh` is the single installer — it now runs
only on the Oracle box, from `infra/oracle/pull-deploy.sh` — so the path a
human tests is the path that runs on every push. It IMPORT-CHECKS
before it restarts — a missing dep fails loudly there rather than as a
crash-loop under `Restart=always` while every EventBridge rule is already
disabled, which is nothing crawling at all.

> **Retention is a MongoDB TTL, not a schedule.** `job_postings.expire_at` is
> stamped in `upsert_posting`'s **`$setOnInsert`** and removed by `pin_posting`;
> the `ttl_expire_at` index deletes the rest. Never move `expire_at` into `$set`
> — a re-crawl would hand a pinned (applied/saved) posting its expiry back and
> the TTL would delete it. The EC2 runner's 05:20 sweep did not run once
> between 2026-09-02 and 2026-09-24 (see the deploy note above) while the DB
> climbed to 4 GB; the daily `retention-watchdog` workflow now repairs and
> FAILS loudly instead. See `docs/ops/manual-steps.md`.

> **Deploys wait for the offline suite on this SHA.** `deploy.yml` polls
> `tests.yml` for `GITHUB_SHA` and refuses on failure, cancel, timeout, or a
> missing run. An older green run cannot authorize a newer commit. Local
> pre-push is optional (`python scripts/install_pre_push_hook.py`); CI is
> the authority. See [`docs/qa/verification.md`](docs/qa/verification.md).

## Config & data
Config via `shared/python/utils/config.py`; secrets via **AWS SSM** in prod (`USE_SSM_SECRETS=true`), `.env` locally (see `.env.example`). Résumé collection is **`user_resumes`** (not `base_resumes`). Résumé files → **S3**. Keep cold starts lean: reuse boto3/Mongo clients at module scope, no heavy import-time work. Deploy lives in-repo: `infra/terraform/` (HTTP API + the 6 Lambdas + shared layer + IAM + KMS credential store + managed mailbox/SES + schedulers) and `scripts/build_layer.sh` + `scripts/package.sh`; see `infra/README.md`. CI: `.github/workflows/tests.yml` (offline suite) + `deploy.yml` (path-filtered per-service deploy).

## Docs
**Start at [`docs/README.md`](docs/README.md)** — it maps the whole tree and
says which directory answers which question. Ten directories, one rule each:
`decisions/` (**ADR-000..007**, every ADR now in one place) · `specs/` ·
`ops/` (runbooks, rollout/rollback, setup, `manual-steps.md`) · `security/` ·
`qa/` · `coverage/` (the measured numbers, with dated snapshots quarantined in
`coverage/reports/`) · `sourcing/` (validated, not-yet-pasted catalog rows) ·
`product/` · `plans/` · `compass/`.

**Quote a coverage figure only from `coverage/`, and regenerate it first.**
Anything under `coverage/reports/` was true on its date and is not current
truth.

## Agents & skills
`.claude/agents/` holds a scoped subagent suite (`orchestrator`, `backend-engineer`, `database-engineer`, `data-scientist`, `security-engineer`, `performance-engineer`, `staff-engineer`, `qa-test-engineer`, `devops-engineer`). Start with **`orchestrator`** for fuzzy asks; see `.claude/agents/README.md`. `.claude/skills/` adds `api-design-principles`, `python-testing-patterns`.

## Agent verification (required)

While implementing: `python scripts/verify.py --fast` (or `--fast -- <selectors>`).
Before declaring ready: `python scripts/verify.py --full`. Fix regressions
this change caused. A later edit that affects checked behavior invalidates
an earlier green run — rerun. Report exact commands and results. Never
claim “all tests pass” from a subset. Never skip, weaken, or suppress
checks to obtain a green result. See [`docs/qa/verification.md`](docs/qa/verification.md).
