# Technology employer coverage

**GENERATED FILE — do not edit by hand.**
Regenerate with:

```bash
PYTHONPATH=shared/python python scripts/employer_coverage_report.py --markdown
```

- measured at `2026-09-03T20:31:27.609150+00:00`
- source revision `14799c3`
- inventory watermark `2026-09-03T20:30:12.450000`
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
| **evidence-backed technology employers** | **2,918** |
| **covered technology employers** | **1,104** |
| **employer coverage** | **37.8%** |
| candidate technology employers (no covered source) | 1,814 |
| covered career SOURCES | 2,606 |
| ownership-verified but unvalidated SOURCES | 259 |
| career sources total | 11,225 |
| employers yielding postings | 1,141 |
| employers yielding core-tech postings | 1,074 |
| active postings from them | 144,514 |
| core-tech postings | 37,990 (26.3%) |
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
| `candidate` | 1,442 |
| `validated` | 1,058 |
| `content_validated` | 296 |
| `dead` | 64 |
| `empty` | 46 |
| `blocked` | 10 |
| `ownership_verified` | 2 |

## Coverage by cohort

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `public_tech` | 1,924 | 415 | 21.6% | 14,851 |
| `niche_startup` | 466 | 288 | 61.8% | 1,179 |
| `unknown` | 464 | 351 | 75.6% | 16,659 |
| `mnc` | 63 | 34 | 54.0% | 9,524 |
| `private_growth` | 37 | 29 | 78.4% | 1,812 |

## Coverage by category

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `software_saas` | 509 | 151 | 29.7% | 4,704 |
| `it_services` | 471 | 92 | 19.5% | 4,257 |
| `other` | 445 | 268 | 60.2% | 15,072 |
| `healthtech` | 273 | 84 | 30.8% | 1,327 |
| `fintech` | 260 | 129 | 49.6% | 6,909 |
| `semiconductor` | 231 | 55 | 23.8% | 2,272 |
| `cloud_data` | 224 | 82 | 36.6% | 2,430 |
| `hardware` | 207 | 31 | 15.0% | 561 |
| `telecom` | 134 | 31 | 23.1% | 657 |
| `ai_ml` | 81 | 62 | 76.5% | 911 |
| `security` | 62 | 40 | 64.5% | 481 |
| `automotive` | 57 | 7 | 12.3% | 39 |
| `devtools` | 53 | 35 | 66.0% | 188 |
| `consumer` | 50 | 29 | 58.0% | 167 |
| `aerospace_defense` | 48 | 22 | 45.8% | 813 |
| `robotics` | 47 | 30 | 63.8% | 668 |
| `gaming` | 27 | 10 | 37.0% | 79 |
| `energy` | 22 | 22 | 100.0% | 612 |
| `research` | 14 | 7 | 50.0% | 3 |
| `media` | 10 | 9 | 90.0% | 76 |
| `ecommerce` | 4 | 2 | 50.0% | 1,397 |

## Coverage by headquarters country

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `US` | 1,890 | 404 | 21.4% | 12,535 |
| `unknown` | 1,028 | 700 | 68.1% | 25,455 |

## Coverage by hiring country

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `unknown` | 2,714 | 900 | 33.2% | 30,176 |
| `US` | 143 | 143 | 100.0% | 5,868 |
| `CA` | 60 | 60 | 100.0% | 3,915 |
| `GB` | 38 | 38 | 100.0% | 3,918 |
| `IN` | 37 | 37 | 100.0% | 3,019 |
| `AU` | 24 | 24 | 100.0% | 3,277 |
| `MX` | 20 | 20 | 100.0% | 2,971 |
| `BR` | 17 | 17 | 100.0% | 2,193 |
| `CO` | 16 | 16 | 100.0% | 2,861 |
| `FR` | 15 | 15 | 100.0% | 2,311 |
| `JP` | 14 | 14 | 100.0% | 3,222 |
| `DE` | 13 | 13 | 100.0% | 2,454 |
| `CN` | 13 | 13 | 100.0% | 977 |
| `IL` | 12 | 12 | 100.0% | 2,618 |
| `MY` | 12 | 12 | 100.0% | 2,570 |
| `PH` | 12 | 12 | 100.0% | 2,398 |
| `ES` | 10 | 10 | 100.0% | 2,595 |
| `CZ` | 9 | 9 | 100.0% | 2,078 |
| `PL` | 9 | 9 | 100.0% | 2,553 |
| `AR` | 8 | 8 | 100.0% | 1,980 |
| `IT` | 8 | 8 | 100.0% | 1,891 |
| `SG` | 8 | 8 | 100.0% | 704 |
| `NL` | 7 | 7 | 100.0% | 2,486 |
| `IE` | 6 | 6 | 100.0% | 616 |
| `PT` | 6 | 6 | 100.0% | 581 |
| `TH` | 6 | 6 | 100.0% | 381 |
| `RO` | 6 | 6 | 100.0% | 648 |
| `CR` | 5 | 5 | 100.0% | 1,896 |
| `VN` | 5 | 5 | 100.0% | 185 |
| `BE` | 4 | 4 | 100.0% | 1,693 |
| `ZA` | 4 | 4 | 100.0% | 1,799 |
| `SA` | 4 | 4 | 100.0% | 516 |
| `TR` | 4 | 4 | 100.0% | 511 |
| `TW` | 4 | 4 | 100.0% | 291 |
| `EG` | 4 | 4 | 100.0% | 297 |
| `KR` | 4 | 4 | 100.0% | 340 |
| `CH` | 3 | 3 | 100.0% | 1,548 |
| `CL` | 3 | 3 | 100.0% | 1,709 |
| `ID` | 3 | 3 | 100.0% | 1,663 |
| `SE` | 3 | 3 | 100.0% | 1,663 |
| `AE` | 3 | 3 | 100.0% | 277 |
| `BG` | 3 | 3 | 100.0% | 494 |
| `PE` | 3 | 3 | 100.0% | 272 |
| `HU` | 3 | 3 | 100.0% | 160 |
| `UY` | 3 | 3 | 100.0% | 71 |
| `HK` | 2 | 2 | 100.0% | 1,663 |
| `DK` | 2 | 2 | 100.0% | 1,401 |
| `FI` | 2 | 2 | 100.0% | 1,548 |
| `NO` | 2 | 2 | 100.0% | 1,548 |
| `NZ` | 2 | 2 | 100.0% | 252 |
| `HR` | 2 | 2 | 100.0% | 476 |
| `RS` | 1 | 1 | 100.0% | 8 |
| `GR` | 1 | 1 | 100.0% | 228 |
| `AT` | 1 | 1 | 100.0% | 151 |

## Coverage by provider

| provider | career sources | ownership verified | validated |
|---|---:|---:|---:|
| `unknown` | 6,515 | 0 | 0 |
| `workday` | 1,519 | 1,053 | 916 |
| `greenhouse` | 1,005 | 751 | 751 |
| `icims` | 455 | 177 | 112 |
| `ashby` | 373 | 296 | 296 |
| `lever` | 190 | 145 | 145 |
| `oracle_orc` | 170 | 60 | 8 |
| `ukg` | 160 | 38 | 38 |
| `paylocity` | 149 | 36 | 36 |
| `smartrecruiters` | 138 | 110 | 110 |
| `bamboohr` | 127 | 32 | 32 |
| `workable` | 96 | 35 | 35 |
| `rippling` | 59 | 16 | 16 |
| `jazzhr` | 51 | 15 | 15 |
| `recruitee` | 48 | 23 | 23 |
| `jobvite` | 42 | 16 | 16 |
| `eightfold` | 39 | 22 | 22 |
| `taleo` | 28 | 5 | 0 |
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
| `baseline` | 94 | 38 | 40.4% |
| `intake-2026-09-03` | 4 | 0 | 0.0% |

| state | employers |
|---|---:|
| `candidate` | 49 |
| `validated` | 37 |
| `not_in_registry` | 9 |
| `empty` | 7 |
| `dead` | 3 |
| `ownership_verified` | 2 |
| `blocked` | 1 |
| `content_validated` | 1 |

| employer | domain | batch | state | sources | detail |
|---|---|---|---|---:|---|
| Google | `google.com` | `baseline` | `candidate` | 1 | domain not verified |
| Apple | `apple.com` | `baseline` | `candidate` | 1 | read 10 page(s) on apple.com; no ATS fingerprint found |
| Meta | `meta.com` | `baseline` | `candidate` | 1 | read 3 page(s) on meta.com; no ATS fingerprint found |
| Microsoft | `microsoft.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| IBM | `ibm.com` | `baseline` | `candidate` | 1 | read 5 page(s) on ibm.com; no ATS fingerprint found |
| SAP | `sap.com` | `baseline` | `candidate` | 2 | domain not verified |
| Salesforce | `salesforce.com` | `baseline` | `candidate` | 1 | read 10 page(s) on salesforce.com; no ATS fingerprint found |
| Tesla | `tesla.com` | `baseline` | `candidate` | 1 | domain not verified |
| Atlassian | `atlassian.com` | `baseline` | `candidate` | 1 | read 10 page(s) on atlassian.com; no ATS fingerprint found |
| Shopify | `shopify.com` | `baseline` | `candidate` | 1 | read 5 page(s) on shopify.com; no ATS fingerprint found |
| DigitalOcean | `digitalocean.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Procore | `procore.com` | `baseline` | `candidate` | 1 | read 7 page(s) on procore.com; no ATS fingerprint found |
| monday.com | `monday.com` | `baseline` | `candidate` | 1 | read 8 page(s) on monday.com; no ATS fingerprint found |
| Fortinet | `fortinet.com` | `baseline` | `candidate` | 1 | domain not verified |
| SentinelOne | `sentinelone.com` | `baseline` | `candidate` | 1 | read 10 page(s) on sentinelone.com; no ATS fingerprint found |
| TSMC | `tsmc.com` | `baseline` | `candidate` | 1 | read 10 page(s) on tsmc.com; no ATS fingerprint found |
| Synopsys | `synopsys.com` | `baseline` | `candidate` | 1 | read 7 page(s) on synopsys.com; no ATS fingerprint found |
| Keysight | `keysight.com` | `baseline` | `candidate` | 4 | domain not verified |
| Teradyne | `teradyne.com` | `baseline` | `candidate` | 1 | read 10 page(s) on teradyne.com; no ATS fingerprint found |
| Garmin | `garmin.com` | `baseline` | `dead` | 1 | migrated to https://careers.garmin.com/careers-home/jobs |
| Dell Technologies | `dell.com` | `baseline` | `candidate` | 1 | read 10 page(s) on dell.com; no ATS fingerprint found |
| Etsy | `etsy.com` | `baseline` | `candidate` | 1 | domain not verified |
| Booking.com | `booking.com` | `baseline` | `candidate` | 3 | domain not verified |
| EPAM Systems | `epam.com` | `baseline` | `candidate` | 1 | domain not verified |
| Rivian | `rivian.com` | `baseline` | `dead` | 1 | migrated to https://careers.rivian.com/jobs |
| Snap | `snap.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Klarna | `klarna.com` | `baseline` | `candidate` | 1 | read 8 page(s) on klarna.com; no ATS fingerprint found |
| Whatnot | `whatnot.com` | `baseline` | `empty` | 2 | verified employer domain -> endpoint (official_domain_link) |
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
| Splunk | `splunk.com` | `baseline` | `blocked` | 1 | read 4 page(s) on splunk.com; no ATS fingerprint found |
| Zoom | `zoom.com` | `baseline` | `candidate` | 1 | read 1 page(s) on zoom.com; no ATS fingerprint found |
| Intuit | `intuit.com` | `baseline` | `candidate` | 1 | read 10 page(s) on intuit.com; no ATS fingerprint found |
| Confluent | `confluent.io` | `baseline` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Arm | `arm.com` | `baseline` | `ownership_verified` | 2 | migrated to https://careers.arm.com/search-jobs |
| Seagate | `seagate.com` | `baseline` | `candidate` | 1 | read 9 page(s) on seagate.com; no ATS fingerprint found |
| Lam Research | `lamresearch.com` | `baseline` | `empty` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Siemens | `siemens.com` | `baseline` | `candidate` | 1 | read 10 page(s) on siemens.com; no ATS fingerprint found |
| Infosys | `infosys.com` | `baseline` | `candidate` | 1 | domain not verified |
| Wipro | `wipro.com` | `baseline` | `candidate` | 1 | read 10 page(s) on wipro.com; no ATS fingerprint found |
| Cognizant | `cognizant.com` | `baseline` | `ownership_verified` | 2 | no portal id configured (see module docstring) |
| Capgemini | `capgemini.com` | `baseline` | `empty` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Accenture | `accenture.com` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Aurora Innovation | `aurora.tech` | `baseline` | `candidate` | 2 | read 5 page(s) on aurora.tech; no ATS fingerprint found |
| Goldman Sachs | `goldmansachs.com` | `baseline` | `candidate` | 1 | read 5 page(s) on goldmansachs.com; no ATS fingerprint found |
| Bloomberg | `bloomberg.com` | `baseline` | `candidate` | 1 | domain not verified |
| Nutanix | `nutanix.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| NetApp | `netapp.com` | `baseline` | `empty` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Akamai | `akamai.com` | `baseline` | `candidate` | 2 | read 8 page(s) on akamai.com; no ATS fingerprint found |
| X | `x.com` | `baseline` | `candidate` | 1 | domain not verified |
| Blue Origin | `blueorigin.com` | `baseline` | `candidate` | 1 | domain not verified |
| Deel | `deel.com` | `baseline` | `candidate` | 1 | read 9 page(s) on deel.com; no ATS fingerprint found |
| Palantir | `palantir.com` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| F5 | `f5.com` | `baseline` | `candidate` | 2 | domain not verified |
| Unity | `unity.com` | `baseline` | `content_validated` | 1 | no ownership evidence gathered |
| SpaceX | `spacex.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| Zoox | `zoox.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| Rippling | `rippling.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, employer_domain |
| Electronic Arts | `ea.com` | `baseline` | `candidate` | 1 | read 10 page(s) on ea.com; no ATS fingerprint found |
| Roblox | `roblox.com` | `baseline` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Riot Games | `riotgames.com` | `baseline` | `candidate` | 1 | read 10 page(s) on riotgames.com; no ATS fingerprint found |
| Epic Games | `epicgames.com` | `baseline` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Take-Two Interactive | `take2games.com` | `baseline` | `candidate` | 1 | read 7 page(s) on take2games.com; no ATS fingerprint found |
| Ubisoft | `ubisoft.com` | `baseline` | `candidate` | 1 | read 10 page(s) on ubisoft.com; no ATS fingerprint found |
| Valve | `valvesoftware.com` | `baseline` | `candidate` | 1 | read 10 page(s) on valvesoftware.com; no ATS fingerprint found |
| Bungie | `bungie.net` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Discord | `discord.com` | `baseline` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| DeepL | `deepl.com` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Pydantic | `pydantic.dev` | `baseline` | `candidate` | 1 | read 5 page(s) on pydantic.dev; no ATS fingerprint found |
| Turso | `turso.tech` | `baseline` | `candidate` | 1 | read 4 page(s) on turso.tech; no ATS fingerprint found |
| Wasmer | `wasmer.io` | `baseline` | `candidate` | 1 | read 4 page(s) on wasmer.io; no ATS fingerprint found |
| Deno | `deno.com` | `baseline` | `empty` | 4 | verified employer domain -> endpoint (official_domain_link) |
| Oven (Bun) | `bun.sh` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Oxide Computer | `oxide.computer` | `baseline` | `candidate` | 1 | read 4 page(s) on oxide.computer; no ATS fingerprint found |
| Aikido Security | `aikido.dev` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Protect AI | `protectai.com` | `baseline` | `dead` | 1 | no working site path (last=422) |
| Lakera | `lakera.ai` | `baseline` | `empty` | 1 | verified employer domain -> endpoint (official_domain_link) |
| HiddenLayer | `hiddenlayer.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Ampere Computing | `amperecomputing.com` | `baseline` | `candidate` | 1 | bot challenge at https://careers.amperecomputing.com/ |
| Celestial AI | `celestial.ai` | `baseline` | `candidate` | 2 | domain not verified |
| d-Matrix | `d-matrix.ai` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Enfabrica | `enfabrica.net` | `baseline` | `candidate` | 1 | read 3 page(s) on enfabrica.net; no ATS fingerprint found |
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
| Safe Superintelligence | `ssi.inc` | `intake-2026-09-03` | `not_in_registry` | 0 | no employer row - the backfill did not mint one |
| Thinking Machines Lab | `thinkingmachines.ai` | `intake-2026-09-03` | `not_in_registry` | 0 | no employer row - the backfill did not mint one |
| Periodic Labs | `periodic.com` | `intake-2026-09-03` | `not_in_registry` | 0 | no employer row - the backfill did not mint one |
| OpenEvidence | `openevidence.com` | `intake-2026-09-03` | `not_in_registry` | 0 | no employer row - the backfill did not mint one |

## Canonical employer attribution

A posting carries `canonical_employer_id` only when the board it came
from has owner-verified evidence naming exactly one employer. Refusing
is a correct answer, so the share is reported against the postings on
owner-verified boards, not against the whole inventory.

| | value |
|---|---:|
| active postings considered | 272,236 |
| on owner-verified boards | 219,212 |
| **carrying a canonical employer** | **200,169** |
| share of owner-verified postings | 91.3% |
| attributing boards | 2,613 |
| contested boards | 151 (75 still blocking) |

| quarantine reason | postings |
|---|---:|
| `ownership_not_verified` | 53,024 |
| `conflicting_owner_verified_sources` | 9,772 |
| `marketplace_or_shared_board` | 7,488 |

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

- registry rows: 109
- **with a verification event: 105**
- never checked: 4
  - `verified`: 63
  - `blocked`: 22
  - `moved`: 10
  - `error`: 4
  - `reachable`: 3
  - `missing`: 3

## Crawl health

| source | shards | crawl lag (min) | stale | shards in backoff |
|---|---:|---:|---|---:|
| `career-pages` | 64 | 8 | False | 0 |
| `icims` | 96 | 11 | False | 0 |
| `oracle` | 48 | 4 | False | 0 |
| `workday` | 48 | 13 | False | 0 |

- ingest runner heartbeat age: 0 min (healthy: True)

