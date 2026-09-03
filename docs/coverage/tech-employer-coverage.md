# Technology employer coverage

**GENERATED FILE — do not edit by hand.**
Regenerate with:

```bash
PYTHONPATH=shared/python python scripts/employer_coverage_report.py --markdown
```

- measured at `2026-09-03T05:49:32.961990+00:00`
- source revision `1d8e500-dirty`
- inventory watermark `2026-09-03T05:49:21.589000`
- posting-mix window: 30 days
- versions: `taxonomy=tech-tax-1.2.0`, `adjacency=tech-adjacency-1.2.0`, `employer_registry=employer-registry-1.0.0`, `career_source=career-source-1.0.0`, `employer_classifier=employer-tech-1.0.0`, `canonical_employer=canonical-employer-1.0.0`, `official_career_registry=official-career-registry-1.0.0`

## What is and is not claimed

This is not a claim to cover every technology company; no denominator
could support that sentence. It is a measured denominator — employers
we hold documentary evidence for — and a numerator that counts a career
source only when its OWNERSHIP is proven and its content validated.

| | value |
|---|---:|
| employers in the registry | 23,791 |
| **evidence-backed technology employers** | **2,829** |
| **covered technology employers** | **1,027** |
| **employer coverage** | **36.3%** |
| candidate technology employers (no covered source) | 1,802 |
| covered career SOURCES | 2,559 |
| ownership-verified but unvalidated SOURCES | 254 |
| career sources total | 11,220 |
| employers yielding postings | 1,049 |
| employers yielding core-tech postings | 964 |
| active postings from them | 125,415 |
| core-tech postings | 27,514 (21.9%) |
| open employer merge reviews | 1,443 |
| open employer board conflicts | 104 |

### Four counts, four units

These are deliberately separated because they were previously one
number under a name that fitted none of them.

- **covered technology employers** — employers, the accountability
  number. One employer, however many boards.
- **covered career sources** — boards. An employer running a Workday
  board and a Greenhouse board is one employer and two sources.
- **ownership-verified but unvalidated sources** — boards whose OWNER
  is proven and whose CONTENT is not yet confirmed. Work in progress,
  not failure, and not coverage.
- **employers yielding postings** — employers we actually have live
  postings from. Never equal to covered: a covered board can be
  genuinely empty, and an uncovered board can still be crawled.

## Every employer is in exactly one state

No employer silently disappears: an employer with no career source at
all is `candidate`, and a provider failure moves a source to `blocked`,
`moved` or `dead` — never out of the denominator.

| state | employers |
|---|---:|
| `candidate` | 1,456 |
| `validated` | 982 |
| `content_validated` | 261 |
| `dead` | 65 |
| `empty` | 45 |
| `blocked` | 12 |
| `ownership_verified` | 8 |

## Coverage by cohort

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `public_tech` | 1,908 | 404 | 21.2% | 11,058 |
| `niche_startup` | 467 | 289 | 61.9% | 1,033 |
| `unknown` | 392 | 287 | 73.2% | 11,663 |
| `mnc` | 62 | 33 | 53.2% | 6,901 |
| `private_growth` | 36 | 27 | 75.0% | 1,471 |

## Coverage by category

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `software_saas` | 509 | 150 | 29.5% | 3,790 |
| `it_services` | 465 | 86 | 18.5% | 2,851 |
| `other` | 381 | 220 | 57.7% | 10,758 |
| `healthtech` | 267 | 78 | 29.2% | 732 |
| `fintech` | 255 | 123 | 48.2% | 5,127 |
| `semiconductor` | 231 | 55 | 23.8% | 1,653 |
| `cloud_data` | 224 | 82 | 36.6% | 1,977 |
| `hardware` | 207 | 31 | 15.0% | 461 |
| `telecom` | 132 | 29 | 22.0% | 468 |
| `ai_ml` | 81 | 62 | 76.5% | 750 |
| `security` | 62 | 40 | 64.5% | 328 |
| `automotive` | 57 | 6 | 10.5% | 32 |
| `devtools` | 53 | 35 | 66.0% | 160 |
| `consumer` | 50 | 28 | 56.0% | 149 |
| `aerospace_defense` | 47 | 21 | 44.7% | 681 |
| `robotics` | 47 | 30 | 63.8% | 618 |
| `gaming` | 26 | 9 | 34.6% | 68 |
| `energy` | 19 | 19 | 100.0% | 342 |
| `research` | 14 | 7 | 50.0% | 3 |
| `media` | 9 | 8 | 88.9% | 54 |
| `ecommerce` | 4 | 1 | 25.0% | 1,142 |

## Coverage by country

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `unknown` | 2,828 | 1,027 | 36.3% | 27,514 |
| `US` | 1 | 0 | 0.0% | 0 |

## Coverage by provider

| provider | career sources | ownership verified | validated |
|---|---:|---:|---:|
| `unknown` | 6,514 | 0 | 0 |
| `workday` | 1,519 | 1,053 | 916 |
| `greenhouse` | 1,003 | 744 | 744 |
| `icims` | 455 | 140 | 79 |
| `ashby` | 372 | 294 | 294 |
| `lever` | 190 | 143 | 143 |
| `oracle_orc` | 170 | 60 | 8 |
| `ukg` | 160 | 38 | 38 |
| `paylocity` | 149 | 36 | 36 |
| `smartrecruiters` | 138 | 109 | 109 |
| `bamboohr` | 127 | 32 | 32 |
| `workable` | 96 | 35 | 35 |
| `rippling` | 59 | 15 | 15 |
| `jazzhr` | 51 | 14 | 14 |
| `recruitee` | 48 | 23 | 23 |
| `jobvite` | 42 | 16 | 16 |
| `eightfold` | 39 | 22 | 22 |
| `taleo` | 27 | 4 | 0 |
| `cornerstone` | 26 | 7 | 7 |
| `successfactors` | 15 | 14 | 14 |
| `neogov` | 12 | 10 | 10 |
| `phenom` | 6 | 2 | 2 |
| `amazon` | 1 | 1 | 1 |
| `radancy` | 1 | 1 | 1 |

## The number this replaces

`company_ats_endpoints.status` records whether an endpoint ANSWERED.
That was previously reported as coverage. It is not coverage — it
cannot distinguish a company's own board from a stranger's board with
a similar slug. Both are shown so the gap is visible.

| endpoint status | count |
|---|---:|
| `validated` | 2,132 |
| `empty` | 296 |
| `dead` | 138 |
| `blocked` | 43 |
| `candidate` | 2 |

## Strategic Tier-A roll call

Every employer in the official career-URL registry, and the state it is
in. An employer missing from this table is the failure the acceptance
criterion names, so the table is built from the REGISTRY rather than
from whatever the database happens to contain.

**The accountable set is the 94 employers the target was set against**, and it is reported separately from anything added
since. A denominator that grows is not a measurement.

| batch | employers | covered | % |
|---|---:|---:|---:|
| `audit-2026-09-02` | 11 | 6 | 54.5% |
| `baseline` | 94 | 37 | 39.4% |

| state | employers |
|---|---:|
| `candidate` | 49 |
| `validated` | 37 |
| `empty` | 6 |
| `not_in_registry` | 5 |
| `blocked` | 4 |
| `dead` | 2 |
| `content_validated` | 1 |
| `ownership_verified` | 1 |

| employer | domain | batch | state | sources | detail |
|---|---|---|---|---:|---|
| Google | `google.com` | `baseline` | `candidate` | 1 | domain not verified |
| Apple | `apple.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on apple.com; no ATS fingerprint found |
| Meta | `meta.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on meta.com; no ATS fingerprint found |
| Microsoft | `microsoft.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| IBM | `ibm.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on ibm.com; no ATS fingerprint found |
| SAP | `sap.com` | `baseline` | `candidate` | 2 | domain not verified |
| Salesforce | `salesforce.com` | `baseline` | `blocked` | 1 | bot challenge at https://www.salesforce.com/ |
| Tesla | `tesla.com` | `baseline` | `candidate` | 1 | domain not verified |
| Atlassian | `atlassian.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on atlassian.com; no ATS fingerprint found |
| Shopify | `shopify.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on shopify.com; no ATS fingerprint found |
| DigitalOcean | `digitalocean.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Procore | `procore.com` | `baseline` | `candidate` | 1 | rendered 3 page(s) on procore.com; no ATS fingerprint found |
| monday.com | `monday.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on monday.com; no ATS fingerprint found |
| Fortinet | `fortinet.com` | `baseline` | `candidate` | 1 | domain not verified |
| SentinelOne | `sentinelone.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on sentinelone.com; no ATS fingerprint found |
| TSMC | `tsmc.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on tsmc.com; no ATS fingerprint found |
| Synopsys | `synopsys.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on synopsys.com; no ATS fingerprint found |
| Keysight | `keysight.com` | `baseline` | `candidate` | 4 | domain not verified |
| Teradyne | `teradyne.com` | `baseline` | `blocked` | 1 | bot challenge at https://www.teradyne.com/ |
| Garmin | `garmin.com` | `baseline` | `dead` | 1 | migrated to https://careers.garmin.com/careers-home/jobs |
| Dell Technologies | `dell.com` | `baseline` | `candidate` | 1 | rendered 2 page(s) on dell.com; no ATS fingerprint found |
| Etsy | `etsy.com` | `baseline` | `candidate` | 1 | domain not verified |
| Booking.com | `booking.com` | `baseline` | `candidate` | 3 | domain not verified |
| EPAM Systems | `epam.com` | `baseline` | `candidate` | 1 | domain not verified |
| Rivian | `rivian.com` | `baseline` | `dead` | 1 | migrated to https://careers.rivian.com/jobs |
| Snap | `snap.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Klarna | `klarna.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on klarna.com; no ATS fingerprint found |
| Whatnot | `whatnot.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on whatnot.com; no ATS fingerprint found |
| Amazon | `amazon.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| Netflix | `netflix.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| Oracle | `oracle.com` | `baseline` | `candidate` | 2 | no ownership evidence gathered |
| Cisco | `cisco.com` | `baseline` | `validated` | 1 | two independent corroborating signals: ats_provider, curated_registry |
| Adobe | `adobe.com` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Intel | `intel.com` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Qualcomm | `qualcomm.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| Broadcom | `broadcom.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| VMware | `vmware.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Uber | `uber.com` | `baseline` | `validated` | 2 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Lyft | `lyft.com` | `baseline` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Airbnb | `airbnb.com` | `baseline` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Spotify | `spotify.com` | `baseline` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| HubSpot | `hubspot.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| Splunk | `splunk.com` | `baseline` | `candidate` | 0 | no career source discovered |
| Zoom | `zoom.com` | `baseline` | `candidate` | 1 | rendered 2 page(s) on zoom.com; no ATS fingerprint found |
| Intuit | `intuit.com` | `baseline` | `blocked` | 1 | rendered 4 page(s) on intuit.com; no ATS fingerprint found |
| Confluent | `confluent.io` | `baseline` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Arm | `arm.com` | `baseline` | `blocked` | 2 | no shell page |
| Seagate | `seagate.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on seagate.com; no ATS fingerprint found |
| Lam Research | `lamresearch.com` | `baseline` | `empty` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Siemens | `siemens.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on siemens.com; no ATS fingerprint found |
| Infosys | `infosys.com` | `baseline` | `candidate` | 1 | domain not verified |
| Wipro | `wipro.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on wipro.com; no ATS fingerprint found |
| Cognizant | `cognizant.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on cognizant.com; no ATS fingerprint found |
| Capgemini | `capgemini.com` | `baseline` | `empty` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Accenture | `accenture.com` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Aurora Innovation | `aurora.tech` | `baseline` | `candidate` | 2 | read 5 page(s) on aurora.tech; no ATS fingerprint found |
| Goldman Sachs | `goldmansachs.com` | `baseline` | `candidate` | 1 | rendered 3 page(s) on goldmansachs.com; no ATS fingerprint found |
| Bloomberg | `bloomberg.com` | `baseline` | `candidate` | 1 | domain not verified |
| Nutanix | `nutanix.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| NetApp | `netapp.com` | `baseline` | `empty` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Akamai | `akamai.com` | `baseline` | `candidate` | 2 | read 8 page(s) on akamai.com; no ATS fingerprint found |
| X | `x.com` | `baseline` | `candidate` | 1 | domain not verified |
| Blue Origin | `blueorigin.com` | `baseline` | `candidate` | 1 | domain not verified |
| Deel | `deel.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on deel.com; no ATS fingerprint found |
| Palantir | `palantir.com` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| F5 | `f5.com` | `baseline` | `candidate` | 2 | domain not verified |
| Unity | `unity.com` | `baseline` | `content_validated` | 1 | no ownership evidence gathered |
| SpaceX | `spacex.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| Zoox | `zoox.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| Rippling | `rippling.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, employer_domain |
| Electronic Arts | `ea.com` | `baseline` | `candidate` | 1 | rendered 3 page(s) on ea.com; no ATS fingerprint found |
| Roblox | `roblox.com` | `baseline` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Riot Games | `riotgames.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on riotgames.com; no ATS fingerprint found |
| Epic Games | `epicgames.com` | `baseline` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Take-Two Interactive | `take2games.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on take2games.com; no ATS fingerprint found |
| Ubisoft | `ubisoft.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on ubisoft.com; no ATS fingerprint found |
| Valve | `valvesoftware.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on valvesoftware.com; no ATS fingerprint found |
| Bungie | `bungie.net` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Discord | `discord.com` | `baseline` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| DeepL | `deepl.com` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Pydantic | `pydantic.dev` | `baseline` | `candidate` | 1 | rendered 4 page(s) on pydantic.dev; no ATS fingerprint found |
| Turso | `turso.tech` | `baseline` | `candidate` | 1 | rendered 4 page(s) on turso.tech; no ATS fingerprint found |
| Wasmer | `wasmer.io` | `baseline` | `candidate` | 1 | rendered 4 page(s) on wasmer.io; no ATS fingerprint found |
| Deno | `deno.com` | `baseline` | `empty` | 4 | verified employer domain -> endpoint (official_domain_link) |
| Oven (Bun) | `bun.sh` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Oxide Computer | `oxide.computer` | `baseline` | `candidate` | 1 | rendered 4 page(s) on oxide.computer; no ATS fingerprint found |
| Aikido Security | `aikido.dev` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Protect AI | `protectai.com` | `baseline` | `ownership_verified` | 1 | no working site path (last=422) |
| Lakera | `lakera.ai` | `baseline` | `empty` | 1 | verified employer domain -> endpoint (official_domain_link) |
| HiddenLayer | `hiddenlayer.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Ampere Computing | `amperecomputing.com` | `baseline` | `candidate` | 1 | rendered 4 page(s) on amperecomputing.com; no ATS fingerprint found |
| Celestial AI | `celestial.ai` | `baseline` | `candidate` | 2 | domain not verified |
| d-Matrix | `d-matrix.ai` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Enfabrica | `enfabrica.net` | `baseline` | `candidate` | 1 | rendered 3 page(s) on enfabrica.net; no ATS fingerprint found |
| xAI | `x.ai` | `audit-2026-09-02` | `not_in_registry` | 0 | no employer row - the backfill did not mint one |
| World Labs | `worldlabs.ai` | `audit-2026-09-02` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Cognition | `cognition.com` | `audit-2026-09-02` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| MotherDuck | `motherduck.com` | `audit-2026-09-02` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Axonius | `axonius.com` | `audit-2026-09-02` | `not_in_registry` | 0 | no employer row - the backfill did not mint one |
| Tines | `tines.com` | `audit-2026-09-02` | `not_in_registry` | 0 | no employer row - the backfill did not mint one |
| Cato Networks | `catonetworks.com` | `audit-2026-09-02` | `not_in_registry` | 0 | no employer row - the backfill did not mint one |
| IonQ | `ionq.com` | `audit-2026-09-02` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Rigetti Computing | `rigetti.com` | `audit-2026-09-02` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Quantinuum | `quantinuum.com` | `audit-2026-09-02` | `empty` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Citadel | `citadel.com` | `audit-2026-09-02` | `not_in_registry` | 0 | no employer row - the backfill did not mint one |

## Canonical employer attribution

A posting carries `canonical_employer_id` only when the board it came
from has owner-verified evidence naming exactly one employer. Refusing
is a correct answer, so the share is reported against the postings on
owner-verified boards, not against the whole inventory.

| | value |
|---|---:|
| active postings considered | 251,739 |
| on owner-verified boards | 200,913 |
| **carrying a canonical employer** | **184,901** |
| share of owner-verified postings | 92.0% |
| attributing boards | 2,579 |
| contested boards | 144 (70 still blocking) |

| quarantine reason | postings |
|---|---:|
| `ownership_not_verified` | 50,826 |
| `conflicting_owner_verified_sources` | 8,594 |
| `marketplace_or_shared_board` | 6,860 |

## Provider readiness

Derived from the REGISTERED provider implementations, not from a
hand-maintained list. The previous list named `usajobs` and
`successfactors` as unsupported while both were implemented and
crawling, so every employer on either vendor was reported as an ATS we
cannot reach.

- registered providers: **27**
- `ready`: 26
- `needs_credential`: 1

| vendor | why |
|---|---|
| `usajobs` | implemented; set `USAJOBS_API_KEY` to enable |

## Official career-URL verification

`verified_at` now records an actual fetch. Before
`scripts/verify_official_career_urls.py` existed, `_row` hard-coded it
to `None` and nothing could ever have written another value.

- registry rows: 105
- **with a verification event: 105**
- never checked: 0
  - `verified`: 63
  - `blocked`: 22
  - `moved`: 10
  - `error`: 4
  - `reachable`: 3
  - `missing`: 3

## Crawl health

| source | shards | crawl lag (min) | stale | shards in backoff |
|---|---:|---:|---|---:|
| `career-pages` | 64 | 11 | False | 0 |
| `icims` | 96 | 0 | False | 0 |
| `oracle` | 48 | 8 | False | 0 |
| `workday` | 48 | 1 | False | 0 |

- ingest runner heartbeat age: 0 min (healthy: True)

