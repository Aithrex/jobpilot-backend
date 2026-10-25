# Technology employer coverage

**GENERATED FILE — do not edit by hand.**
Regenerate with:

```bash
PYTHONPATH=shared/python python scripts/employer_coverage_report.py --markdown
```

- measured at `2026-10-25T18:05:41.480001+00:00`
- source revision `981110b-dirty`
- inventory watermark `2026-10-25T18:04:35.253000`
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
| **evidence-backed technology employers** | **2,978** |
| **covered technology employers** | **1,296** |
| **employer coverage** | **43.5%** |
| candidate technology employers (no covered source) | 1,682 |
| covered career SOURCES | 2,932 |
| ownership-verified but unvalidated SOURCES | 195 |
| career sources total | 11,617 |
| employers yielding postings | 1,102 |
| employers yielding core-tech postings | 1,015 |
| active postings from them | 116,669 |
| core-tech postings | 33,371 (28.6%) |
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
| `candidate` | 1,420 |
| `validated` | 1,245 |
| `content_validated` | 208 |
| `empty` | 51 |
| `dead` | 51 |
| `ownership_verified` | 2 |
| `blocked` | 1 |

## Coverage by cohort

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `public_tech` | 1,942 | 535 | 27.6% | 13,889 |
| `niche_startup` | 525 | 371 | 70.7% | 949 |
| `unknown` | 446 | 338 | 75.8% | 14,554 |
| `mnc` | 62 | 40 | 64.5% | 7,276 |
| `private_growth` | 38 | 30 | 79.0% | 1,549 |

## Coverage by category

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `other` | 519 | 352 | 67.8% | 13,302 |
| `software_saas` | 509 | 175 | 34.4% | 3,825 |
| `it_services` | 467 | 112 | 24.0% | 3,087 |
| `healthtech` | 276 | 100 | 36.2% | 1,545 |
| `fintech` | 256 | 136 | 53.1% | 5,477 |
| `semiconductor` | 231 | 69 | 29.9% | 3,093 |
| `cloud_data` | 224 | 88 | 39.3% | 1,654 |
| `hardware` | 207 | 49 | 23.7% | 545 |
| `telecom` | 131 | 33 | 25.2% | 521 |
| `ai_ml` | 81 | 65 | 80.2% | 756 |
| `security` | 62 | 42 | 67.7% | 467 |
| `automotive` | 57 | 14 | 24.6% | 51 |
| `devtools` | 53 | 38 | 71.7% | 188 |
| `aerospace_defense` | 50 | 27 | 54.0% | 822 |
| `consumer` | 50 | 34 | 68.0% | 114 |
| `robotics` | 47 | 35 | 74.5% | 630 |
| `gaming` | 26 | 10 | 38.5% | 72 |
| `energy` | 25 | 25 | 100.0% | 604 |
| `research` | 14 | 8 | 57.1% | 5 |
| `media` | 10 | 10 | 100.0% | 220 |
| `ecommerce` | 4 | 2 | 50.0% | 713 |

## Coverage by headquarters country

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `US` | 1,907 | 518 | 27.2% | 12,281 |
| `unknown` | 1,071 | 778 | 72.6% | 21,090 |

## Coverage by hiring country

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `unknown` | 2,787 | 1,105 | 39.6% | 27,472 |
| `US` | 135 | 135 | 100.0% | 4,506 |
| `CA` | 57 | 57 | 100.0% | 2,575 |
| `GB` | 35 | 35 | 100.0% | 2,369 |
| `IN` | 30 | 30 | 100.0% | 1,816 |
| `AU` | 23 | 23 | 100.0% | 2,102 |
| `MX` | 18 | 18 | 100.0% | 1,842 |
| `BR` | 16 | 16 | 100.0% | 1,266 |
| `CO` | 16 | 16 | 100.0% | 1,691 |
| `JP` | 13 | 13 | 100.0% | 1,941 |
| `DE` | 12 | 12 | 100.0% | 1,293 |
| `FR` | 12 | 12 | 100.0% | 1,313 |
| `MY` | 12 | 12 | 100.0% | 1,660 |
| `CN` | 12 | 12 | 100.0% | 1,007 |
| `IL` | 11 | 11 | 100.0% | 1,554 |
| `PH` | 11 | 11 | 100.0% | 1,408 |
| `CZ` | 9 | 9 | 100.0% | 1,279 |
| `ES` | 8 | 8 | 100.0% | 1,393 |
| `PL` | 8 | 8 | 100.0% | 1,426 |
| `SG` | 8 | 8 | 100.0% | 572 |
| `NL` | 7 | 7 | 100.0% | 1,468 |
| `AR` | 6 | 6 | 100.0% | 1,000 |
| `IT` | 6 | 6 | 100.0% | 967 |
| `IE` | 6 | 6 | 100.0% | 475 |
| `TH` | 6 | 6 | 100.0% | 307 |
| `CR` | 5 | 5 | 100.0% | 1,104 |
| `EG` | 5 | 5 | 100.0% | 267 |
| `PT` | 5 | 5 | 100.0% | 316 |
| `RO` | 5 | 5 | 100.0% | 376 |
| `AE` | 4 | 4 | 100.0% | 284 |
| `TR` | 4 | 4 | 100.0% | 328 |
| `TW` | 4 | 4 | 100.0% | 282 |
| `VN` | 4 | 4 | 100.0% | 264 |
| `BE` | 3 | 3 | 100.0% | 919 |
| `CH` | 3 | 3 | 100.0% | 799 |
| `CL` | 3 | 3 | 100.0% | 961 |
| `ID` | 3 | 3 | 100.0% | 894 |
| `SE` | 3 | 3 | 100.0% | 894 |
| `ZA` | 3 | 3 | 100.0% | 930 |
| `BG` | 3 | 3 | 100.0% | 390 |
| `SA` | 3 | 3 | 100.0% | 321 |
| `KR` | 3 | 3 | 100.0% | 117 |
| `HU` | 3 | 3 | 100.0% | 93 |
| `UY` | 3 | 3 | 100.0% | 65 |
| `HK` | 2 | 2 | 100.0% | 834 |
| `DK` | 2 | 2 | 100.0% | 725 |
| `FI` | 2 | 2 | 100.0% | 799 |
| `NO` | 2 | 2 | 100.0% | 799 |
| `NZ` | 2 | 2 | 100.0% | 224 |
| `HR` | 2 | 2 | 100.0% | 269 |
| `RS` | 1 | 1 | 100.0% | 25 |
| `GR` | 1 | 1 | 100.0% | 209 |
| `PE` | 1 | 1 | 100.0% | 95 |
| `AT` | 1 | 1 | 100.0% | 86 |

## Coverage by provider

| provider | career sources | ownership verified | validated |
|---|---:|---:|---:|
| `unknown` | 6,528 | 0 | 0 |
| `workday` | 1,561 | 1,105 | 1,016 |
| `greenhouse` | 1,092 | 796 | 795 |
| `icims` | 464 | 198 | 129 |
| `ashby` | 421 | 321 | 321 |
| `lever` | 211 | 151 | 151 |
| `oracle_orc` | 180 | 58 | 27 |
| `ukg` | 165 | 41 | 41 |
| `paylocity` | 152 | 38 | 38 |
| `smartrecruiters` | 144 | 115 | 115 |
| `bamboohr` | 127 | 32 | 32 |
| `workable` | 106 | 43 | 43 |
| `rippling` | 60 | 17 | 17 |
| `jazzhr` | 53 | 22 | 22 |
| `recruitee` | 50 | 25 | 25 |
| `jobvite` | 43 | 17 | 17 |
| `successfactors` | 41 | 38 | 38 |
| `eightfold` | 39 | 22 | 22 |
| `adp_wfn` | 32 | 19 | 19 |
| `taleo` | 30 | 6 | 1 |
| `cornerstone` | 27 | 8 | 8 |
| `careers_page` | 21 | 0 | 0 |
| `phenom` | 13 | 9 | 9 |
| `neogov` | 12 | 10 | 10 |
| `jibe` | 12 | 12 | 12 |
| `radancy` | 8 | 7 | 7 |
| `paycom` | 6 | 4 | 4 |
| `breezy` | 6 | 6 | 6 |
| `paycor` | 5 | 4 | 4 |
| `amazon` | 1 | 1 | 1 |
| `custom_tiktok` | 1 | 0 | 0 |
| `goldman_sachs` | 1 | 0 | 0 |
| `custom_ibm` | 1 | 0 | 0 |
| `custom_apple` | 1 | 1 | 1 |
| `google_careers` | 1 | 0 | 0 |
| `custom_atlassian` | 1 | 0 | 0 |
| `avature` | 1 | 1 | 1 |

## The number this replaces

`company_ats_endpoints.status` records whether an endpoint ANSWERED.
That was previously reported as coverage. It is not coverage — it
cannot distinguish a company's own board from a stranger's board with
a similar slug. Both are shown so the gap is visible.

| endpoint status | count |
|---|---:|
| `validated` | 6,424 |
| `empty` | 866 |
| `dead` | 282 |
| `blocked` | 113 |
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
| `baseline` | 94 | 48 | 51.1% |
| `intake-2026-09-03` | 4 | 0 | 0.0% |

| state | employers |
|---|---:|
| `validated` | 45 |
| `candidate` | 39 |
| `empty` | 9 |
| `not_in_registry` | 9 |
| `content_validated` | 6 |
| `dead` | 1 |

| employer | domain | batch | state | sources | detail |
|---|---|---|---|---:|---|
| Google | `google.com` | `baseline` | `content_validated` | 2 | only non-probative signals present (a responding endpoint and a similar slug are not owner |
| Apple | `apple.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| Meta | `meta.com` | `baseline` | `candidate` | 1 | read 3 page(s) on meta.com; no ATS fingerprint found |
| Microsoft | `microsoft.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| IBM | `ibm.com` | `baseline` | `content_validated` | 2 | one corroborating signal (employer_domain); the rule needs two independent classes |
| SAP | `sap.com` | `baseline` | `empty` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Salesforce | `salesforce.com` | `baseline` | `candidate` | 1 | read 10 page(s) on salesforce.com; no ATS fingerprint found |
| Tesla | `tesla.com` | `baseline` | `candidate` | 1 | bot challenge at https://www.tesla.com/ |
| Atlassian | `atlassian.com` | `baseline` | `content_validated` | 3 | only non-probative signals present (a responding endpoint and a similar slug are not owner |
| Shopify | `shopify.com` | `baseline` | `candidate` | 1 | read 6 page(s) on shopify.com; no ATS fingerprint found |
| DigitalOcean | `digitalocean.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Procore | `procore.com` | `baseline` | `candidate` | 1 | read 6 page(s) on procore.com; no ATS fingerprint found |
| monday.com | `monday.com` | `baseline` | `content_validated` | 2 | one corroborating signal (employer_domain); the rule needs two independent classes |
| Fortinet | `fortinet.com` | `baseline` | `candidate` | 1 | domain not verified |
| SentinelOne | `sentinelone.com` | `baseline` | `candidate` | 1 | read 10 page(s) on sentinelone.com; no ATS fingerprint found |
| TSMC | `tsmc.com` | `baseline` | `candidate` | 1 | domain not verified |
| Synopsys | `synopsys.com` | `baseline` | `candidate` | 1 | read 7 page(s) on synopsys.com; no ATS fingerprint found |
| Keysight | `keysight.com` | `baseline` | `validated` | 5 | verified employer domain -> endpoint (official_domain_link) |
| Teradyne | `teradyne.com` | `baseline` | `candidate` | 1 | read 10 page(s) on teradyne.com; no ATS fingerprint found |
| Garmin | `garmin.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Dell Technologies | `dell.com` | `baseline` | `candidate` | 2 | read 10 page(s) on dell.com; no ATS fingerprint found |
| Etsy | `etsy.com` | `baseline` | `candidate` | 1 | bot challenge at https://www.etsy.com/ |
| Booking.com | `booking.com` | `baseline` | `validated` | 4 | verified employer domain -> endpoint (official_domain_link) |
| EPAM Systems | `epam.com` | `baseline` | `candidate` | 1 | bot challenge at https://www.epam.com/ |
| Rivian | `rivian.com` | `baseline` | `validated` | 3 | verified employer domain -> endpoint (official_domain_link) |
| Snap | `snap.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Klarna | `klarna.com` | `baseline` | `candidate` | 1 | bot challenge at https://careers.klarna.com/ |
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
| Splunk | `splunk.com` | `baseline` | `candidate` | 1 | read 5 page(s) on splunk.com; no ATS fingerprint found |
| Zoom | `zoom.com` | `baseline` | `candidate` | 1 | read 7 page(s) on zoom.com; no ATS fingerprint found |
| Intuit | `intuit.com` | `baseline` | `candidate` | 1 | read 10 page(s) on intuit.com; no ATS fingerprint found |
| Confluent | `confluent.io` | `baseline` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Arm | `arm.com` | `baseline` | `dead` | 2 | migrated to https://careers.arm.com/search-jobs |
| Seagate | `seagate.com` | `baseline` | `candidate` | 1 | read 9 page(s) on seagate.com; no ATS fingerprint found |
| Lam Research | `lamresearch.com` | `baseline` | `empty` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Siemens | `siemens.com` | `baseline` | `candidate` | 1 | read 10 page(s) on siemens.com; no ATS fingerprint found |
| Infosys | `infosys.com` | `baseline` | `candidate` | 1 | read 10 page(s) on infosys.com; no ATS fingerprint found |
| Wipro | `wipro.com` | `baseline` | `content_validated` | 2 | only non-probative signals present (a responding endpoint and a similar slug are not owner |
| Cognizant | `cognizant.com` | `baseline` | `candidate` | 2 | rendered 4 page(s) on cognizant.com; no ATS fingerprint found |
| Capgemini | `capgemini.com` | `baseline` | `empty` | 3 | verified employer domain -> endpoint (official_domain_link) |
| Accenture | `accenture.com` | `baseline` | `validated` | 1 | two independent corroborating signals: ats_provider, curated_registry |
| Aurora Innovation | `aurora.tech` | `baseline` | `candidate` | 2 | read 5 page(s) on aurora.tech; no ATS fingerprint found |
| Goldman Sachs | `goldmansachs.com` | `baseline` | `content_validated` | 2 | only non-probative signals present (a responding endpoint and a similar slug are not owner |
| Bloomberg | `bloomberg.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Nutanix | `nutanix.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| NetApp | `netapp.com` | `baseline` | `validated` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Akamai | `akamai.com` | `baseline` | `candidate` | 2 | read 8 page(s) on akamai.com; no ATS fingerprint found |
| X | `x.com` | `baseline` | `candidate` | 2 | domain not verified |
| Blue Origin | `blueorigin.com` | `baseline` | `candidate` | 1 | domain not verified |
| Deel | `deel.com` | `baseline` | `candidate` | 1 | read 8 page(s) on deel.com; no ATS fingerprint found |
| Palantir | `palantir.com` | `baseline` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| F5 | `f5.com` | `baseline` | `empty` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Unity | `unity.com` | `baseline` | `validated` | 1 | two independent corroborating signals: ats_provider, curated_registry |
| SpaceX | `spacex.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| Zoox | `zoox.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| Rippling | `rippling.com` | `baseline` | `validated` | 2 | two independent corroborating signals: ats_provider, employer_domain |
| Electronic Arts | `ea.com` | `baseline` | `candidate` | 1 | read 10 page(s) on ea.com; no ATS fingerprint found |
| Roblox | `roblox.com` | `baseline` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Riot Games | `riotgames.com` | `baseline` | `candidate` | 1 | read 10 page(s) on riotgames.com; no ATS fingerprint found |
| Epic Games | `epicgames.com` | `baseline` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Take-Two Interactive | `take2games.com` | `baseline` | `candidate` | 1 | read 7 page(s) on take2games.com; no ATS fingerprint found |
| Ubisoft | `ubisoft.com` | `baseline` | `candidate` | 1 | read 10 page(s) on ubisoft.com; no ATS fingerprint found |
| Valve | `valvesoftware.com` | `baseline` | `candidate` | 1 | read 8 page(s) on valvesoftware.com; no ATS fingerprint found |
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
| Protect AI | `protectai.com` | `baseline` | `candidate` | 1 | provider names a different owner: Career Site |
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
| active postings considered | 303,074 |
| on owner-verified boards | 183,603 |
| **carrying a canonical employer** | **168,160** |
| share of owner-verified postings | 91.6% |
| attributing boards | 2,853 |
| contested boards | 167 (79 still blocking) |

| quarantine reason | postings |
|---|---:|
| `ownership_not_verified` | 119,471 |
| `marketplace_or_shared_board` | 4,243 |
| `conflicting_owner_verified_sources` | 2,934 |

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
| `career-pages` | 64 | 12 | False | 0 |
| `icims` | 96 | 1 | False | 0 |
| `oracle` | 48 | 9 | False | 0 |
| `workday` | 48 | 3 | False | 0 |

- ingest runner heartbeat age: 0 min (healthy: True)

