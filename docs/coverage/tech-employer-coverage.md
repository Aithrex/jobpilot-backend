# Technology employer coverage

**GENERATED FILE — do not edit by hand.**
Regenerate with:

```bash
PYTHONPATH=shared/python python scripts/employer_coverage_report.py --markdown
```

- measured at `2026-10-23T00:17:02.584840+00:00`
- source revision `1057f73-dirty`
- inventory watermark `2026-10-23T00:15:14.600000`
- posting-mix window: 30 days
- versions: `taxonomy=tech-tax-1.3.0`, `adjacency=tech-adjacency-1.3.0`, `employer_registry=employer-registry-1.0.0`, `career_source=career-source-1.0.0`, `employer_classifier=employer-tech-1.0.0`, `canonical_employer=canonical-employer-1.0.0`, `official_career_registry=official-career-registry-1.0.0`

## What is and is not claimed

This is not a claim to cover every technology company; no denominator
could support that sentence. It is a measured denominator — employers
we hold documentary evidence for — and a numerator that counts a career
source only when its OWNERSHIP is proven and its content validated.

| | value |
|---|---:|
| employers in the registry | 23,846 |
| **evidence-backed technology employers** | **3,028** |
| **covered technology employers** | **1,213** |
| **employer coverage** | **40.1%** |
| candidate technology employers (no covered source) | 1,815 |
| covered career SOURCES | 2,710 |
| ownership-verified but unvalidated SOURCES | 257 |
| career sources total | 11,309 |
| employers yielding postings | 1,157 |
| employers yielding core-tech postings | 1,078 |
| active postings from them | 141,258 |
| core-tech postings | 39,334 (27.9%) |
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
| `candidate` | 1,466 |
| `validated` | 1,165 |
| `content_validated` | 258 |
| `dead` | 70 |
| `empty` | 48 |
| `ownership_verified` | 11 |
| `blocked` | 10 |

## Coverage by cohort

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `public_tech` | 1,949 | 447 | 22.9% | 15,612 |
| `niche_startup` | 524 | 349 | 66.6% | 1,051 |
| `unknown` | 489 | 365 | 74.6% | 17,846 |
| `mnc` | 62 | 36 | 58.1% | 8,418 |
| `private_growth` | 39 | 31 | 79.5% | 1,767 |

## Coverage by category

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `other` | 560 | 371 | 66.2% | 15,944 |
| `software_saas` | 509 | 155 | 30.4% | 4,379 |
| `it_services` | 467 | 89 | 19.1% | 4,046 |
| `healthtech` | 281 | 92 | 32.7% | 2,148 |
| `fintech` | 259 | 128 | 49.4% | 6,175 |
| `semiconductor` | 231 | 57 | 24.7% | 3,157 |
| `cloud_data` | 224 | 85 | 38.0% | 1,853 |
| `hardware` | 207 | 32 | 15.5% | 670 |
| `telecom` | 131 | 28 | 21.4% | 662 |
| `ai_ml` | 81 | 62 | 76.5% | 857 |
| `security` | 62 | 41 | 66.1% | 539 |
| `automotive` | 57 | 7 | 12.3% | 51 |
| `devtools` | 53 | 35 | 66.0% | 207 |
| `consumer` | 50 | 29 | 58.0% | 140 |
| `aerospace_defense` | 49 | 24 | 49.0% | 866 |
| `robotics` | 47 | 31 | 66.0% | 717 |
| `gaming` | 26 | 9 | 34.6% | 75 |
| `energy` | 26 | 26 | 100.0% | 777 |
| `research` | 14 | 7 | 50.0% | 5 |
| `media` | 11 | 10 | 90.9% | 243 |
| `ecommerce` | 4 | 2 | 50.0% | 973 |

## Coverage by headquarters country

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `US` | 1,914 | 433 | 22.6% | 13,617 |
| `unknown` | 1,114 | 780 | 70.0% | 25,717 |

## Coverage by hiring country

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `unknown` | 2,830 | 1,015 | 35.9% | 32,004 |
| `US` | 141 | 141 | 100.0% | 5,606 |
| `CA` | 59 | 59 | 100.0% | 3,290 |
| `GB` | 37 | 37 | 100.0% | 3,184 |
| `IN` | 33 | 33 | 100.0% | 2,300 |
| `AU` | 23 | 23 | 100.0% | 2,676 |
| `MX` | 19 | 19 | 100.0% | 2,549 |
| `BR` | 18 | 18 | 100.0% | 1,695 |
| `CO` | 17 | 17 | 100.0% | 2,385 |
| `FR` | 15 | 15 | 100.0% | 1,741 |
| `DE` | 14 | 14 | 100.0% | 1,840 |
| `JP` | 14 | 14 | 100.0% | 2,682 |
| `MY` | 12 | 12 | 100.0% | 2,251 |
| `CN` | 12 | 12 | 100.0% | 1,169 |
| `IL` | 11 | 11 | 100.0% | 2,155 |
| `PH` | 11 | 11 | 100.0% | 1,971 |
| `ES` | 10 | 10 | 100.0% | 2,002 |
| `CZ` | 9 | 9 | 100.0% | 1,655 |
| `PL` | 9 | 9 | 100.0% | 2,030 |
| `AR` | 8 | 8 | 100.0% | 1,379 |
| `IT` | 8 | 8 | 100.0% | 1,299 |
| `SG` | 8 | 8 | 100.0% | 702 |
| `NL` | 7 | 7 | 100.0% | 2,064 |
| `IE` | 6 | 6 | 100.0% | 592 |
| `PT` | 6 | 6 | 100.0% | 384 |
| `TH` | 6 | 6 | 100.0% | 355 |
| `RO` | 6 | 6 | 100.0% | 629 |
| `CR` | 5 | 5 | 100.0% | 1,478 |
| `EG` | 5 | 5 | 100.0% | 342 |
| `VN` | 5 | 5 | 100.0% | 266 |
| `ZA` | 4 | 4 | 100.0% | 1,276 |
| `AE` | 4 | 4 | 100.0% | 389 |
| `SA` | 4 | 4 | 100.0% | 423 |
| `TR` | 4 | 4 | 100.0% | 428 |
| `TW` | 4 | 4 | 100.0% | 350 |
| `KR` | 4 | 4 | 100.0% | 157 |
| `BE` | 3 | 3 | 100.0% | 1,215 |
| `CH` | 3 | 3 | 100.0% | 1,084 |
| `CL` | 3 | 3 | 100.0% | 1,299 |
| `ID` | 3 | 3 | 100.0% | 1,196 |
| `SE` | 3 | 3 | 100.0% | 1,196 |
| `BG` | 3 | 3 | 100.0% | 504 |
| `HU` | 3 | 3 | 100.0% | 119 |
| `UY` | 3 | 3 | 100.0% | 78 |
| `HK` | 2 | 2 | 100.0% | 1,165 |
| `DK` | 2 | 2 | 100.0% | 987 |
| `FI` | 2 | 2 | 100.0% | 1,084 |
| `NO` | 2 | 2 | 100.0% | 1,084 |
| `NZ` | 2 | 2 | 100.0% | 309 |
| `PE` | 2 | 2 | 100.0% | 124 |
| `HR` | 2 | 2 | 100.0% | 484 |
| `RS` | 1 | 1 | 100.0% | 21 |
| `GR` | 1 | 1 | 100.0% | 281 |
| `AT` | 1 | 1 | 100.0% | 111 |

## Coverage by provider

| provider | career sources | ownership verified | validated |
|---|---:|---:|---:|
| `unknown` | 6,515 | 0 | 0 |
| `workday` | 1,528 | 1,062 | 926 |
| `greenhouse` | 1,014 | 760 | 760 |
| `icims` | 459 | 192 | 124 |
| `ashby` | 384 | 309 | 309 |
| `lever` | 190 | 146 | 146 |
| `oracle_orc` | 177 | 66 | 18 |
| `ukg` | 163 | 41 | 41 |
| `paylocity` | 149 | 36 | 36 |
| `smartrecruiters` | 140 | 112 | 112 |
| `bamboohr` | 127 | 32 | 32 |
| `workable` | 98 | 37 | 37 |
| `rippling` | 59 | 16 | 16 |
| `jazzhr` | 52 | 20 | 20 |
| `recruitee` | 48 | 23 | 23 |
| `jobvite` | 43 | 17 | 17 |
| `eightfold` | 39 | 22 | 22 |
| `successfactors` | 37 | 37 | 37 |
| `taleo` | 29 | 6 | 1 |
| `cornerstone` | 26 | 7 | 7 |
| `neogov` | 12 | 10 | 10 |
| `phenom` | 11 | 7 | 7 |
| `radancy` | 7 | 7 | 7 |
| `amazon` | 1 | 1 | 1 |
| `adp_wfn` | 1 | 1 | 1 |

## The number this replaces

`company_ats_endpoints.status` records whether an endpoint ANSWERED.
That was previously reported as coverage. It is not coverage — it
cannot distinguish a company's own board from a stranger's board with
a similar slug. Both are shown so the gap is visible.

| endpoint status | count |
|---|---:|
| `validated` | 6,361 |
| `empty` | 858 |
| `dead` | 282 |
| `blocked` | 112 |
| `candidate` | 22 |

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
| `baseline` | 94 | 41 | 43.6% |
| `intake-2026-09-03` | 4 | 0 | 0.0% |

| state | employers |
|---|---:|
| `candidate` | 45 |
| `validated` | 38 |
| `empty` | 9 |
| `not_in_registry` | 9 |
| `ownership_verified` | 5 |
| `dead` | 2 |
| `content_validated` | 1 |

| employer | domain | batch | state | sources | detail |
|---|---|---|---|---:|---|
| Google | `google.com` | `baseline` | `candidate` | 1 | read 2 page(s) on google.com; no ATS fingerprint found |
| Apple | `apple.com` | `baseline` | `candidate` | 1 | read 10 page(s) on apple.com; no ATS fingerprint found |
| Meta | `meta.com` | `baseline` | `candidate` | 1 | read 3 page(s) on meta.com; no ATS fingerprint found |
| Microsoft | `microsoft.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| IBM | `ibm.com` | `baseline` | `candidate` | 1 | read 5 page(s) on ibm.com; no ATS fingerprint found |
| SAP | `sap.com` | `baseline` | `empty` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Salesforce | `salesforce.com` | `baseline` | `candidate` | 1 | read 10 page(s) on salesforce.com; no ATS fingerprint found |
| Tesla | `tesla.com` | `baseline` | `candidate` | 1 | bot challenge at https://www.tesla.com/ |
| Atlassian | `atlassian.com` | `baseline` | `candidate` | 1 | read 10 page(s) on atlassian.com; no ATS fingerprint found |
| Shopify | `shopify.com` | `baseline` | `candidate` | 1 | read 5 page(s) on shopify.com; no ATS fingerprint found |
| DigitalOcean | `digitalocean.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Procore | `procore.com` | `baseline` | `candidate` | 1 | read 6 page(s) on procore.com; no ATS fingerprint found |
| monday.com | `monday.com` | `baseline` | `candidate` | 1 | read 8 page(s) on monday.com; no ATS fingerprint found |
| Fortinet | `fortinet.com` | `baseline` | `candidate` | 1 | domain not verified |
| SentinelOne | `sentinelone.com` | `baseline` | `candidate` | 1 | read 10 page(s) on sentinelone.com; no ATS fingerprint found |
| TSMC | `tsmc.com` | `baseline` | `candidate` | 1 | read 10 page(s) on tsmc.com; no ATS fingerprint found |
| Synopsys | `synopsys.com` | `baseline` | `candidate` | 1 | read 7 page(s) on synopsys.com; no ATS fingerprint found |
| Keysight | `keysight.com` | `baseline` | `ownership_verified` | 4 | migrated to https://jobs.keysight.com/jobs |
| Teradyne | `teradyne.com` | `baseline` | `candidate` | 1 | read 10 page(s) on teradyne.com; no ATS fingerprint found |
| Garmin | `garmin.com` | `baseline` | `dead` | 1 | migrated to https://careers.garmin.com/careers-home/jobs |
| Dell Technologies | `dell.com` | `baseline` | `candidate` | 1 | read 10 page(s) on dell.com; no ATS fingerprint found |
| Etsy | `etsy.com` | `baseline` | `candidate` | 1 | bot challenge at https://www.etsy.com/ |
| Booking.com | `booking.com` | `baseline` | `ownership_verified` | 3 | migrated to https://jobs.booking.com/booking/jobs |
| EPAM Systems | `epam.com` | `baseline` | `candidate` | 1 | bot challenge at https://www.epam.com/ |
| Rivian | `rivian.com` | `baseline` | `dead` | 1 | migrated to https://careers.rivian.com/jobs |
| Snap | `snap.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Klarna | `klarna.com` | `baseline` | `candidate` | 1 | read 8 page(s) on klarna.com; no ATS fingerprint found |
| Whatnot | `whatnot.com` | `baseline` | `empty` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Amazon | `amazon.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| Netflix | `netflix.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| Oracle | `oracle.com` | `baseline` | `empty` | 2 | verified employer domain -> endpoint (official_domain_link) |
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
| Splunk | `splunk.com` | `baseline` | `candidate` | 1 | read 4 page(s) on splunk.com; no ATS fingerprint found |
| Zoom | `zoom.com` | `baseline` | `candidate` | 1 | read 1 page(s) on zoom.com; no ATS fingerprint found |
| Intuit | `intuit.com` | `baseline` | `candidate` | 1 | read 10 page(s) on intuit.com; no ATS fingerprint found |
| Confluent | `confluent.io` | `baseline` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Arm | `arm.com` | `baseline` | `ownership_verified` | 2 | migrated to https://careers.arm.com/search-jobs |
| Seagate | `seagate.com` | `baseline` | `candidate` | 1 | read 9 page(s) on seagate.com; no ATS fingerprint found |
| Lam Research | `lamresearch.com` | `baseline` | `empty` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Siemens | `siemens.com` | `baseline` | `candidate` | 1 | read 10 page(s) on siemens.com; no ATS fingerprint found |
| Infosys | `infosys.com` | `baseline` | `candidate` | 1 | bot challenge at https://www.infosys.com/ |
| Wipro | `wipro.com` | `baseline` | `candidate` | 1 | read 10 page(s) on wipro.com; no ATS fingerprint found |
| Cognizant | `cognizant.com` | `baseline` | `ownership_verified` | 2 | only non-probative signals present (a responding endpoint and a similar slug are not owner |
| Capgemini | `capgemini.com` | `baseline` | `empty` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Accenture | `accenture.com` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Aurora Innovation | `aurora.tech` | `baseline` | `candidate` | 2 | read 5 page(s) on aurora.tech; no ATS fingerprint found |
| Goldman Sachs | `goldmansachs.com` | `baseline` | `candidate` | 1 | read 5 page(s) on goldmansachs.com; no ATS fingerprint found |
| Bloomberg | `bloomberg.com` | `baseline` | `candidate` | 1 | bot challenge at https://www.bloomberg.com/ |
| Nutanix | `nutanix.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| NetApp | `netapp.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Akamai | `akamai.com` | `baseline` | `ownership_verified` | 2 | no ownership evidence gathered |
| X | `x.com` | `baseline` | `candidate` | 2 | domain not verified |
| Blue Origin | `blueorigin.com` | `baseline` | `candidate` | 1 | domain not verified |
| Deel | `deel.com` | `baseline` | `candidate` | 1 | read 9 page(s) on deel.com; no ATS fingerprint found |
| Palantir | `palantir.com` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| F5 | `f5.com` | `baseline` | `empty` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Unity | `unity.com` | `baseline` | `content_validated` | 1 | no working site path (last=404) |
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
| Protect AI | `protectai.com` | `baseline` | `candidate` | 1 | no ownership evidence gathered |
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
| active postings considered | 356,102 |
| on owner-verified boards | 224,861 |
| **carrying a canonical employer** | **210,606** |
| share of owner-verified postings | 93.7% |
| attributing boards | 2,706 |
| contested boards | 156 (79 still blocking) |

| quarantine reason | postings |
|---|---:|
| `ownership_not_verified` | 131,241 |
| `marketplace_or_shared_board` | 4,679 |
| `conflicting_owner_verified_sources` | 3,395 |

## Provider readiness

Derived from the REGISTERED provider implementations, not from a
hand-maintained list. The previous list named `usajobs` and
`successfactors` as unsupported while both were implemented and
crawling, so every employer on either vendor was reported as an ATS we
cannot reach.

- registered providers: **42**
- `ready`: 39
- `parked`: 2
- `needs_credential`: 1

| vendor | why |
|---|---|
| `adp_myjobs` | observed 2026-10-22: myjobs.adp.com is a JS shell; the career-site config is public (owner named) but the listing request is built at runtime and could not be established without a browser; unverified, not empty |
| `dayforce` | observed 2026-10-22: public listing call POST jobs.dayforcehcm.com/api/geo/<ns>/jobposting/search returns 403 to non-browser clients (Cloudflare bot management); blocked, not empty |
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
| `career-pages` | 64 | 9 | False | 0 |
| `icims` | 96 | 11 | False | 0 |
| `oracle` | 48 | 5 | False | 0 |
| `workday` | 48 | 1 | False | 0 |

- ingest runner heartbeat age: 0 min (healthy: True)

