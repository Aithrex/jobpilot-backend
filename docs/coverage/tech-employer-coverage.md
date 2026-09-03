# Technology employer coverage

**GENERATED FILE — do not edit by hand.**
Regenerate with:

```bash
PYTHONPATH=shared/python python scripts/employer_coverage_report.py --markdown
```

- measured at `2026-09-03T22:04:55.098755+00:00`
- source revision `d71b01d`
- inventory watermark `2026-09-03T22:04:18.367000`
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
| **evidence-backed technology employers** | **2,923** |
| **covered technology employers** | **1,116** |
| **employer coverage** | **38.2%** |
| candidate technology employers (no covered source) | 1,807 |
| covered career SOURCES | 2,616 |
| ownership-verified but unvalidated SOURCES | 259 |
| career sources total | 11,225 |
| employers yielding postings | 1,147 |
| employers yielding core-tech postings | 1,079 |
| active postings from them | 147,385 |
| core-tech postings | 38,786 (26.3%) |
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
| `candidate` | 1,436 |
| `validated` | 1,070 |
| `content_validated` | 304 |
| `dead` | 59 |
| `empty` | 46 |
| `blocked` | 6 |
| `ownership_verified` | 2 |

## Coverage by cohort

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `public_tech` | 1,925 | 421 | 21.9% | 15,087 |
| `unknown` | 468 | 357 | 76.3% | 17,097 |
| `niche_startup` | 466 | 288 | 61.8% | 1,205 |
| `mnc` | 63 | 34 | 54.0% | 9,670 |
| `private_growth` | 37 | 29 | 78.4% | 1,854 |

## Coverage by category

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `software_saas` | 509 | 151 | 29.7% | 4,772 |
| `it_services` | 471 | 93 | 19.8% | 4,334 |
| `other` | 449 | 277 | 61.7% | 15,409 |
| `healthtech` | 273 | 84 | 30.8% | 1,370 |
| `fintech` | 261 | 130 | 49.8% | 7,042 |
| `semiconductor` | 231 | 55 | 23.8% | 2,319 |
| `cloud_data` | 224 | 82 | 36.6% | 2,471 |
| `hardware` | 207 | 31 | 15.0% | 568 |
| `telecom` | 134 | 31 | 23.1% | 667 |
| `ai_ml` | 81 | 62 | 76.5% | 939 |
| `security` | 62 | 40 | 64.5% | 484 |
| `automotive` | 57 | 7 | 12.3% | 39 |
| `devtools` | 53 | 35 | 66.0% | 191 |
| `consumer` | 50 | 29 | 58.0% | 170 |
| `aerospace_defense` | 48 | 23 | 47.9% | 834 |
| `robotics` | 47 | 30 | 63.8% | 685 |
| `gaming` | 27 | 10 | 37.0% | 79 |
| `energy` | 22 | 22 | 100.0% | 647 |
| `research` | 14 | 7 | 50.0% | 3 |
| `media` | 10 | 9 | 90.0% | 78 |
| `ecommerce` | 4 | 2 | 50.0% | 1,417 |

## Coverage by headquarters country

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `US` | 1,891 | 410 | 21.7% | 12,736 |
| `unknown` | 1,032 | 706 | 68.4% | 26,050 |

## Coverage by hiring country

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `unknown` | 2,717 | 910 | 33.5% | 30,757 |
| `US` | 144 | 144 | 100.0% | 6,035 |
| `CA` | 60 | 60 | 100.0% | 3,978 |
| `GB` | 38 | 38 | 100.0% | 4,009 |
| `IN` | 37 | 37 | 100.0% | 3,074 |
| `AU` | 24 | 24 | 100.0% | 3,358 |
| `MX` | 20 | 20 | 100.0% | 3,014 |
| `BR` | 18 | 18 | 100.0% | 2,288 |
| `CO` | 17 | 17 | 100.0% | 2,919 |
| `FR` | 15 | 15 | 100.0% | 2,350 |
| `JP` | 14 | 14 | 100.0% | 3,271 |
| `DE` | 13 | 13 | 100.0% | 2,489 |
| `CN` | 13 | 13 | 100.0% | 993 |
| `IL` | 12 | 12 | 100.0% | 2,652 |
| `MY` | 12 | 12 | 100.0% | 2,600 |
| `PH` | 12 | 12 | 100.0% | 2,435 |
| `ES` | 10 | 10 | 100.0% | 2,634 |
| `CZ` | 9 | 9 | 100.0% | 2,108 |
| `PL` | 9 | 9 | 100.0% | 2,585 |
| `AR` | 8 | 8 | 100.0% | 2,013 |
| `IT` | 8 | 8 | 100.0% | 1,927 |
| `SG` | 8 | 8 | 100.0% | 718 |
| `NL` | 7 | 7 | 100.0% | 2,516 |
| `IE` | 6 | 6 | 100.0% | 646 |
| `PT` | 6 | 6 | 100.0% | 589 |
| `TH` | 6 | 6 | 100.0% | 400 |
| `RO` | 6 | 6 | 100.0% | 654 |
| `CR` | 5 | 5 | 100.0% | 1,926 |
| `EG` | 5 | 5 | 100.0% | 360 |
| `VN` | 5 | 5 | 100.0% | 185 |
| `BE` | 4 | 4 | 100.0% | 1,718 |
| `ZA` | 4 | 4 | 100.0% | 1,821 |
| `AE` | 4 | 4 | 100.0% | 341 |
| `SA` | 4 | 4 | 100.0% | 525 |
| `TR` | 4 | 4 | 100.0% | 520 |
| `TW` | 4 | 4 | 100.0% | 300 |
| `KR` | 4 | 4 | 100.0% | 345 |
| `CH` | 3 | 3 | 100.0% | 1,573 |
| `CL` | 3 | 3 | 100.0% | 1,738 |
| `ID` | 3 | 3 | 100.0% | 1,691 |
| `SE` | 3 | 3 | 100.0% | 1,691 |
| `BG` | 3 | 3 | 100.0% | 504 |
| `PE` | 3 | 3 | 100.0% | 275 |
| `HU` | 3 | 3 | 100.0% | 165 |
| `UY` | 3 | 3 | 100.0% | 71 |
| `HK` | 2 | 2 | 100.0% | 1,684 |
| `DK` | 2 | 2 | 100.0% | 1,421 |
| `FI` | 2 | 2 | 100.0% | 1,573 |
| `NO` | 2 | 2 | 100.0% | 1,573 |
| `NZ` | 2 | 2 | 100.0% | 255 |
| `HR` | 2 | 2 | 100.0% | 476 |
| `RS` | 1 | 1 | 100.0% | 8 |
| `GR` | 1 | 1 | 100.0% | 230 |
| `AT` | 1 | 1 | 100.0% | 156 |

## Coverage by provider

| provider | career sources | ownership verified | validated |
|---|---:|---:|---:|
| `unknown` | 6,515 | 0 | 0 |
| `workday` | 1,519 | 1,053 | 916 |
| `greenhouse` | 1,005 | 751 | 751 |
| `icims` | 455 | 183 | 118 |
| `ashby` | 373 | 296 | 296 |
| `lever` | 190 | 145 | 145 |
| `oracle_orc` | 170 | 60 | 8 |
| `ukg` | 160 | 38 | 38 |
| `paylocity` | 149 | 36 | 36 |
| `smartrecruiters` | 138 | 110 | 110 |
| `bamboohr` | 127 | 32 | 32 |
| `workable` | 96 | 35 | 35 |
| `rippling` | 59 | 16 | 16 |
| `jazzhr` | 51 | 19 | 19 |
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
| `candidate` | 50 |
| `validated` | 37 |
| `not_in_registry` | 9 |
| `empty` | 7 |
| `dead` | 3 |
| `ownership_verified` | 2 |
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
| Splunk | `splunk.com` | `baseline` | `candidate` | 1 | read 4 page(s) on splunk.com; no ATS fingerprint found |
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
| active postings considered | 275,410 |
| on owner-verified boards | 222,196 |
| **carrying a canonical employer** | **202,817** |
| share of owner-verified postings | 91.3% |
| attributing boards | 2,619 |
| contested boards | 153 (77 still blocking) |

| quarantine reason | postings |
|---|---:|
| `ownership_not_verified` | 53,214 |
| `conflicting_owner_verified_sources` | 9,910 |
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
| `career-pages` | 64 | 12 | False | 0 |
| `icims` | 96 | 0 | False | 0 |
| `oracle` | 48 | 8 | False | 0 |
| `workday` | 48 | 2 | False | 0 |

- ingest runner heartbeat age: 0 min (healthy: True)

