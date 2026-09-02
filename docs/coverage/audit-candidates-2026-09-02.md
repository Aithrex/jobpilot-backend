# The 15 audit candidates, probed

The brief named fifteen companies to audit and said: *verify current company
status, ownership, canonical domain and public hiring availability before
adding them*. This is that verification, run live on 2026-09-02.

Two checks per company, both from `career_source_ownership`:

* **domain confirmed** — `verify_domain` fetches the homepage and asks whether
  the page NAMES the company in visible text. Matching markup would be
  circular; a page that mentions the name only inside an `href` has not said
  who it is.
* **careers URL** — the HTTP status of the career landing URL in
  `official_career_registry`.

| company | domain | domain confirmed | careers URL | reading |
|---|---|---|---|---|
| DeepL | `deepl.com` | yes | 200 | hiring publicly |
| Pydantic | `pydantic.dev` | yes | 200 | hiring publicly |
| Turso | `turso.tech` | yes | 200 | hiring publicly |
| Wasmer | `wasmer.io` | yes | 200 | hiring publicly |
| Deno | `deno.com` | yes | 200 | hiring publicly |
| Oven (Bun) | `bun.sh` | yes | 200 | hiring publicly; legal entity is Oven, product is Bun |
| Oxide Computer | `oxide.computer` | yes | 200 | hiring publicly |
| Aikido Security | `aikido.dev` | yes | 200 | hiring publicly |
| HiddenLayer | `hiddenlayer.com` | yes | 200 | hiring publicly |
| Lakera | `lakera.ai` | yes | 200 | hiring publicly |
| d-Matrix | `d-matrix.ai` | yes | 200 | hiring publicly |
| Celestial AI | `celestial.ai` | **no** | 200 | careers page answers; the HOMEPAGE does not name the company in visible text (a JS shell), so our own domain check cannot confirm it |
| Ampere Computing | `amperecomputing.com` | yes | **403** | bot wall. `blocked`, not `empty` |
| Protect AI | `protectai.com` | yes | **404** | careers path gone |
| Enfabrica | `enfabrica.net` | yes | **404** | careers path moved |

All fifteen are in the `employers` registry with a domain-derived
`employer_id`.

## What is deliberately NOT claimed

Three of these companies are widely reported to have been acquired. **This
document does not assert any acquisition**, and none of them has
`operating_status: acquired` in the registry. What was measured is a 404, and
a 404 has several causes — a path change, a rebuild, a temporary outage — of
which an acquisition is only one.

Guessing `acquired` from a 404 is the same class of error as guessing an owner
from a slug: a confident answer produced by a heuristic, written into a field
that reads as a fact. `operating_status` moves on evidence — a redirect to the
acquirer's careers site, or a curated row — and until then these stay `active`
with the measurement recorded.

The same rule was applied backwards: an earlier draft of
`official_career_registry` carried notes asserting two of these acquisitions
and one for VMware. All three were written from memory rather than from a
check, and all three have been replaced with what was measured.

## What to do with each

| reading | action |
|---|---|
| 200 + domain confirmed | nothing. Pass 2 will walk from the domain to the board on the next run. |
| 403 (Ampere) | `blocked` with a 3-day recheck. Do not mark `empty` — the board exists. |
| 404 (Protect AI, Enfabrica) | re-fingerprint from the DOMAIN rather than the stored path: `verify_career_sources.py --mode employers --employer-id <id>`. The domain is confirmed, so the walk can find the new path. |
| domain unconfirmed (Celestial AI) | the check is working as intended. A JS-shell homepage is not evidence of anything, and the fallback — `candidate_domains_from_board` — needs a board first. Leave as `candidate`. |

Reproduce:

```bash
PYTHONPATH=shared/python python scripts/verify_career_sources.py \
    --mode employers --employer-id <id> ...
```
