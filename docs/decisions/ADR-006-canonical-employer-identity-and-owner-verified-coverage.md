# ADR-006 — Canonical employer identity, and coverage that means ownership

**Status:** accepted, 2026-09-02
**Supersedes the coverage definition in** ADR-004 sec. 3 and
`docs/coverage/employer-coverage-definition.md` sec. 2.

## The two defects

### 1. Five registries, no shared identifier

`companies` keys on `sha1(name|state)`. `job_postings.employer_id` is a **slug
of whatever name the crawler saw**. `career_pages_catalog` keys on `(ats,
slug)`, `workday_tenant_catalog` on `(vendor, tenant)`,
`company_ats_endpoints` on `member_key`, and `scripts/data/tech_companies.py`
on a display name. None of them joins to any other.

Measured on live `aspirely_db`, 2026-09-01/02:

* 129 `(ats, tenant)` pairs carried MORE THAN ONE `employer_name`, covering
  28,386 active postings — 12.5% of the whole active inventory.
  `oracle_orc/jpmc` alone was published under "NUTMEG CONTAINER CORPORATION",
  "CHASE FAMILY LIMITED PARTNERSHIP NO. 5" and "JPMorgan Chase".
* NVIDIA had 672 active postings crawled from `workday_tenant_catalog` while
  its `companies` row read `discovery_status: no_portal, endpoints_found: 0`.
  Any coverage number computed from `companies` alone was wrong in a direction
  that looked like failure.
* `stripe` matched "STRIPES SOLUTIONS LLC", `notion` matched "NOTIONSBYNANCY
  LLC", `ramp` matched "RAMPART RANGE MOTORCYCLE MANAGEMENT COMMITTEE", and
  Capital One was given the domain `capital.com` — a CFD trading site.

`employer_identity.resolve_employer_name` patched the *display* of this at
read time. It could not fix the *key*, because there was no key.

### 2. "Validated" meant "the endpoint answered"

`company_ats_endpoints.status` went to `validated` when a request succeeded.
2,111 of 2,592 rows were `validated`, and a representative one read:

```json
{"member_key": "ashby-uipath", "sources": ["discovery", "slug-probe"],
 "status": "validated", "tenant": "uipath", "job_count_last": 5}
```

No field in that document answers *how do we know this board is UiPath's*,
because the answer was "we guessed the slug and it returned five jobs". The
only gate was `ats_providers.util.slug_is_trustworthy`, a string-similarity
function. It cannot separate:

| slug | employers it could belong to |
|---|---|
| `forward` | Forward (healthcare) / Forward Networks / Forward Air |
| `beyond` | Beyond Inc / Beyond Meat / Beyond Finance |
| `public` | Public.com / Public Storage / Public Consulting Group |
| `cts` | CTS Corp / Cognizant Technology Solutions / CTS Cement |
| `super` | Super.com / Super Micro / SuperAnnotate |
| `archer` | Archer Aviation / Archer Daniels Midland / ArcherDX |
| `costar` | the Greenhouse board declares "Co-Star", an astrology app |
| `alphabet` | Alphabet Inc / ALPHABET, BMW's fleet-leasing arm |

Reporting that as coverage meant reporting a number that could not distinguish
a company's own board from a stranger's.

## Decision

### One canonical `employer_id`

A new `employers` collection, keyed `emp_<12 hex>`, derived deterministically
so the same employer computes the same id in any process with no round trip:

```
verified domain  ->  sha1("domain:" + registrable_domain)     confidence 0.95
curated key      ->  sha1("curated:" + key)                   confidence 0.80
name             ->  sha1("name:" + name_key)                 confidence 0.45
```

Domain-first because a domain is the one employer attribute that is globally
unique, independently verifiable and stable across a rename. `rekey_to_domain`
promotes a name-derived employer when a domain is later verified, leaving the
old id behind as a `merged` row pointing at the new one, so no stored
reference dangles.

The document carries `employer_id`, `display_name`, `legal_names[]`,
`aliases[]`, `brands[]`, `parent_employer_id`, `operating_status`,
`canonical_domain`, `verified_domains[]`, `employer_categories[]`, `cohorts[]`,
`countries[]`, `evidence[]`, `first_seen_at`, `last_verified_at` and
`next_verification_at`.

`operating_status` is `active | renamed | acquired | merged | closed |
product_or_brand`. These are load-bearing: `merged`/`acquired` carry
`parent_employer_id` and the resolver follows the pointer, so an acquisition
does not delete an employer from the denominator, and `product_or_brand` is
how Slack stops being an employer separate from Salesforce while its postings
stay attributed.

**Two employers are never merged on a normalized name.** `merge_decision`
returns `merge` only for a shared verified domain, a curated alias/parent row,
or the same owner-verified `(vendor, tenant)`; `distinct` for two different
verified domains; and `quarantine` — written to `employer_merge_reviews` — for
everything that merely looks like a match, including an exact name match. On
this dataset an exact name match is wrong often enough to have broken five
well-known employers.

### Coverage means ownership, and it is a separate collection

A new `career_sources` collection with a nine-state machine:

```
candidate  ownership_verified  content_validated  validated
empty  blocked  moved  dead  manual_review
```

Only `validated` and `empty` count as covered. `empty` counts because an
owner-verified board with nothing on it is a verified fact about the employer,
not a gap in our crawl.

Ownership is proven by EITHER:

* **(a)** a link or redirect chain from an independently verified official
  employer domain to the exact endpoint — sufficient alone; or
* **(b)** two **independent** corroborating signals, where independence is by
  CLASS: `employer_domain`, `ats_provider`, `curated_registry`. Two signals of
  the same class are one signal repeated.

`slug_similarity` and `endpoint_responds` are in the vocabulary and are
**never counted**. Recording and then ignoring them is deliberate: a report
can say "we checked, and declined to count it", which a silent omission
cannot.

A provider that names a DIFFERENT owner is decisive AGAINST ownership — that
is the CoStar case, where the board declares an astrology app and
`names_match` normalises the two identically.

### Probing is dry-run by default

`scripts/verify_career_sources.py` and
`scripts/backfill_employer_registry.py` write nothing without `--apply`.
Discovering an endpoint and applying it are different decisions; conflating
them is what wrote `validated` onto a guess in the first place.

## Consequences

**The coverage number goes down before it goes up, and that is the point.**
The old 43.0% measured "the endpoint answered". The new number measures "we
can show whose board this is". The two are reported side by side in
`docs/coverage/tech-employer-coverage.md` so the gap is visible rather than
being the reason a number looked good.

Additive, and reversible: `employers` and `career_sources` are new
collections. `companies`, `company_ats_endpoints` and `job_postings` are
untouched, the crawler keeps reading exactly the rows it read before, and the
whole migration reverses by dropping two collections.

Two vendors gained an owner check they did not have. Lever and Ashby publish
no owner field on their JSON APIs — which is precisely why they were the two
vendors where a guessed slug could reach `validated` on string similarity
alone — but both render the employer's name into their public portal title.
One cheap GET closes it (`_owner_from_portal_title`).

For an employer that exists only as an ATS `display_name` and has no domain,
`candidate_domains_from_board` reads the domain the BOARD links to and hands
it to the same independent verification. A board link on its own is one page's
assertion about itself; a board link whose destination names the employer is
two parties agreeing.

## What is still not claimed

Not "every technology company". No denominator supports that sentence. What
exists is a dated, measurable denominator and a numerator that only counts
proven ownership, both stamped with the source revision and the inventory
watermark that produced them, regenerable by one command:

```bash
PYTHONPATH=shared/python python scripts/employer_coverage_report.py --markdown
```
