# Technology employer coverage

**GENERATED FILE — do not edit by hand.**
Regenerate with:

```bash
PYTHONPATH=shared/python python scripts/employer_coverage_report.py --markdown
```

- measured at `2026-09-02T11:58:49.998075+00:00`
- source revision `d495687`
- inventory watermark `2026-09-02T11:56:38.316000`
- posting-mix window: 30 days
- versions: `taxonomy=tech-tax-1.1.0`, `adjacency=tech-adjacency-1.1.0`, `employer_registry=employer-registry-1.0.0`, `career_source=career-source-1.0.0`, `employer_classifier=employer-tech-1.0.0`

## What is and is not claimed

This is not a claim to cover every technology company; no denominator
could support that sentence. It is a measured denominator — employers
we hold documentary evidence for — and a numerator that counts a career
source only when its OWNERSHIP is proven and its content validated.

| | value |
|---|---:|
| employers in the registry | 23,782 |
| **evidence-backed technology employers** | **2,624** |
| **validated official career sources** | **596** |
| **validated coverage** | **22.7%** |
| employers without validated coverage | 2,028 |
| employers yielding postings | 844 |
| employers yielding core-tech postings | 689 |
| active postings from them | 97,687 |
| core-tech postings | 15,773 (16.2%) |
| open employer merge reviews | 1,443 |

## Every employer is in exactly one state

No employer silently disappears: an employer with no career source at
all is `candidate`, and a provider failure moves a source to `blocked`,
`moved` or `dead` — never out of the denominator.

| state | employers |
|---|---:|
| `candidate` | 1,742 |
| `validated` | 523 |
| `content_validated` | 146 |
| `blocked` | 102 |
| `empty` | 73 |
| `ownership_verified` | 38 |

## Coverage by cohort

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `public_tech` | 1,859 | 309 | 16.6% | 6,674 |
| `niche_startup` | 465 | 264 | 56.8% | 584 |
| `unknown` | 240 | 2 | 0.8% | 6,291 |
| `mnc` | 58 | 8 | 13.8% | 4,433 |
| `private_growth` | 33 | 17 | 51.5% | 809 |

## Coverage by category

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `other` | 2,475 | 563 | 22.8% | 15,773 |
| `software_saas` | 138 | 67 | 48.5% | 2,193 |
| `fintech` | 122 | 38 | 31.1% | 3,322 |
| `cloud_data` | 91 | 52 | 57.1% | 1,052 |
| `semiconductor` | 81 | 28 | 34.6% | 969 |
| `ai_ml` | 78 | 57 | 73.1% | 491 |
| `security` | 61 | 38 | 62.3% | 191 |
| `devtools` | 51 | 33 | 64.7% | 92 |
| `it_services` | 50 | 11 | 22.0% | 1,423 |
| `consumer` | 49 | 23 | 46.9% | 82 |
| `robotics` | 47 | 26 | 55.3% | 308 |
| `healthtech` | 39 | 21 | 53.8% | 226 |
| `gaming` | 26 | 10 | 38.5% | 45 |
| `aerospace_defense` | 15 | 8 | 53.3% | 355 |
| `energy` | 12 | 4 | 33.3% | 129 |
| `hardware` | 10 | 2 | 20.0% | 243 |
| `media` | 7 | 3 | 42.9% | 19 |
| `telecom` | 6 | 2 | 33.3% | 300 |
| `automotive` | 5 | 0 | 0.0% | 7 |
| `ecommerce` | 4 | 0 | 0.0% | 552 |

## Coverage by country

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `unknown` | 2,623 | 596 | 22.7% | 15,773 |
| `US` | 1 | 0 | 0.0% | 0 |

## Coverage by provider

| provider | career sources | ownership verified | validated |
|---|---:|---:|---:|
| `unknown` | 6,514 | 0 | 0 |
| `workday` | 682 | 194 | 43 |
| `greenhouse` | 657 | 346 | 346 |
| `icims` | 446 | 118 | 57 |
| `ashby` | 245 | 138 | 138 |
| `phenom` | 199 | 192 | 192 |
| `oracle_orc` | 160 | 46 | 4 |
| `ukg` | 157 | 38 | 38 |
| `paylocity` | 148 | 36 | 36 |
| `lever` | 130 | 64 | 64 |
| `bamboohr` | 126 | 32 | 32 |
| `smartrecruiters` | 73 | 31 | 31 |
| `workable` | 65 | 18 | 18 |
| `rippling` | 56 | 12 | 12 |
| `jazzhr` | 50 | 13 | 13 |
| `jobvite` | 49 | 23 | 23 |
| `eightfold` | 35 | 18 | 18 |
| `taleo` | 24 | 3 | 0 |
| `cornerstone` | 24 | 7 | 7 |
| `recruitee` | 23 | 3 | 3 |
| `successfactors` | 13 | 13 | 13 |
| `neogov` | 2 | 0 | 0 |

## The number this replaces

`company_ats_endpoints.status` records whether an endpoint ANSWERED.
That was previously reported as coverage. It is not coverage — it
cannot distinguish a company's own board from a stranger's board with
a similar slug. Both are shown so the gap is visible.

| endpoint status | count |
|---|---:|
| `validated` | 2,131 |
| `empty` | 301 |
| `dead` | 138 |
| `blocked` | 44 |
| `candidate` | 2 |

## Strategic Tier-A roll call

Every employer in the official career-URL registry, and the state it is
in. An employer missing from this table is the failure the acceptance
criterion names, so the table is built from the REGISTRY rather than
from whatever the database happens to contain.

| state | employers |
|---|---:|
| `candidate` | 68 |
| `validated` | 14 |
| `empty` | 7 |
| `ownership_verified` | 4 |
| `blocked` | 1 |

| employer | domain | state | sources | detail |
|---|---|---|---:|---|
| Google | `google.com` | `candidate` | 1 | domain not verified |
| Apple | `apple.com` | `candidate` | 1 | read 8 page(s) on apple.com; no ATS fingerprint found |
| Meta | `meta.com` | `candidate` | 1 | read 3 page(s) on meta.com; no ATS fingerprint found |
| Microsoft | `microsoft.com` | `candidate` | 1 | read 4 page(s) on microsoft.com; no ATS fingerprint found |
| IBM | `ibm.com` | `candidate` | 1 | read 5 page(s) on ibm.com; no ATS fingerprint found |
| SAP | `sap.com` | `candidate` | 1 | domain not verified |
| Salesforce | `salesforce.com` | `candidate` | 1 | read 8 page(s) on salesforce.com; no ATS fingerprint found |
| Tesla | `tesla.com` | `candidate` | 1 | domain not verified |
| Atlassian | `atlassian.com` | `candidate` | 1 | read 8 page(s) on atlassian.com; no ATS fingerprint found |
| Shopify | `shopify.com` | `candidate` | 1 | read 4 page(s) on shopify.com; no ATS fingerprint found |
| DigitalOcean | `digitalocean.com` | `candidate` | 1 | read 4 page(s) on digitalocean.com; no ATS fingerprint found |
| Procore | `procore.com` | `candidate` | 1 | read 1 page(s) on procore.com; no ATS fingerprint found |
| monday.com | `monday.com` | `candidate` | 1 | read 7 page(s) on monday.com; no ATS fingerprint found |
| Fortinet | `fortinet.com` | `candidate` | 1 | domain not verified |
| SentinelOne | `sentinelone.com` | `candidate` | 1 | read 8 page(s) on sentinelone.com; no ATS fingerprint found |
| TSMC | `tsmc.com` | `candidate` | 1 | domain not verified |
| Synopsys | `synopsys.com` | `candidate` | 1 | read 7 page(s) on synopsys.com; no ATS fingerprint found |
| Keysight | `keysight.com` | `candidate` | 1 | domain not verified |
| Teradyne | `teradyne.com` | `candidate` | 1 | read 8 page(s) on teradyne.com; no ATS fingerprint found |
| Garmin | `garmin.com` | `ownership_verified` | 1 | migrated to https://careers.garmin.com/careers-home/jobs |
| Dell Technologies | `dell.com` | `candidate` | 1 | read 8 page(s) on dell.com; no ATS fingerprint found |
| Etsy | `etsy.com` | `candidate` | 1 | domain not verified |
| Booking.com | `booking.com` | `candidate` | 1 | domain not verified |
| EPAM Systems | `epam.com` | `candidate` | 1 | domain not verified |
| Rivian | `rivian.com` | `ownership_verified` | 1 | migrated to https://careers.rivian.com/jobs |
| Snap | `snap.com` | `candidate` | 1 | read 8 page(s) on snap.com; no ATS fingerprint found |
| Klarna | `klarna.com` | `candidate` | 1 | read 8 page(s) on klarna.com; no ATS fingerprint found |
| Whatnot | `whatnot.com` | `candidate` | 1 | domain not verified |
| Amazon | `amazon.com` | `candidate` | 1 | read 8 page(s) on amazon.com; no ATS fingerprint found |
| Netflix | `netflix.com` | `candidate` | 1 | domain not verified |
| Oracle | `oracle.com` | `ownership_verified` | 2 | no host |
| Cisco | `cisco.com` | `empty` | 5 | verified employer domain -> endpoint (official_domain_link) |
| Adobe | `adobe.com` | `empty` | 5 | verified employer domain -> endpoint (official_domain_link) |
| Intel | `intel.com` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Qualcomm | `qualcomm.com` | `candidate` | 1 | read 4 page(s) on qualcomm.com; no ATS fingerprint found |
| Broadcom | `broadcom.com` | `candidate` | 1 | read 4 page(s) on broadcom.com; no ATS fingerprint found |
| VMware | `vmware.com` | `candidate` | 1 | read 3 page(s) on vmware.com; no ATS fingerprint found |
| Uber | `uber.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Lyft | `lyft.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Airbnb | `airbnb.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Spotify | `spotify.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| HubSpot | `hubspot.com` | `candidate` | 1 | read 8 page(s) on hubspot.com; no ATS fingerprint found |
| Splunk | `splunk.com` | `empty` | 5 | verified employer domain -> endpoint (official_domain_link) |
| Zoom | `zoom.com` | `candidate` | 1 | read 3 page(s) on zoom.com; no ATS fingerprint found |
| Intuit | `intuit.com` | `candidate` | 1 | read 8 page(s) on intuit.com; no ATS fingerprint found |
| Confluent | `confluent.io` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Arm | `arm.com` | `ownership_verified` | 2 | migrated to https://careers.arm.com/search-jobs |
| Seagate | `seagate.com` | `candidate` | 1 | read 7 page(s) on seagate.com; no ATS fingerprint found |
| Lam Research | `lamresearch.com` | `empty` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Siemens | `siemens.com` | `candidate` | 1 | read 8 page(s) on siemens.com; no ATS fingerprint found |
| Infosys | `infosys.com` | `candidate` | 1 | read 8 page(s) on infosys.com; no ATS fingerprint found |
| Wipro | `wipro.com` | `candidate` | 1 | read 8 page(s) on wipro.com; no ATS fingerprint found |
| Cognizant | `cognizant.com` | `candidate` | 1 | read 6 page(s) on cognizant.com; no ATS fingerprint found |
| Capgemini | `capgemini.com` | `candidate` | 1 | read 8 page(s) on capgemini.com; no ATS fingerprint found |
| Accenture | `accenture.com` | `candidate` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Aurora Innovation | `aurora.tech` | `candidate` | 1 | read 5 page(s) on aurora.tech; no ATS fingerprint found |
| Goldman Sachs | `goldmansachs.com` | `candidate` | 1 | read 5 page(s) on goldmansachs.com; no ATS fingerprint found |
| Bloomberg | `bloomberg.com` | `candidate` | 1 | domain not verified |
| Nutanix | `nutanix.com` | `candidate` | 1 | read 1 page(s) on nutanix.com; no ATS fingerprint found |
| NetApp | `netapp.com` | `empty` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Akamai | `akamai.com` | `candidate` | 1 | read 8 page(s) on akamai.com; no ATS fingerprint found |
| X | `x.com` | `candidate` | 1 | domain not verified |
| Blue Origin | `blueorigin.com` | `candidate` | 1 | domain not verified |
| Deel | `deel.com` | `candidate` | 1 | read 8 page(s) on deel.com; no ATS fingerprint found |
| Palantir | `palantir.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| F5 | `f5.com` | `candidate` | 1 | domain not verified |
| Unity | `unity.com` | `candidate` | 1 | no working site path (last=404) |
| SpaceX | `spacex.com` | `candidate` | 1 | read 8 page(s) on spacex.com; no ATS fingerprint found |
| Zoox | `zoox.com` | `candidate` | 1 | read 3 page(s) on zoox.com; no ATS fingerprint found |
| Rippling | `rippling.com` | `candidate` | 1 | read 6 page(s) on rippling.com; no ATS fingerprint found |
| Electronic Arts | `ea.com` | `candidate` | 1 | read 8 page(s) on ea.com; no ATS fingerprint found |
| Roblox | `roblox.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Riot Games | `riotgames.com` | `candidate` | 1 | read 8 page(s) on riotgames.com; no ATS fingerprint found |
| Epic Games | `epicgames.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Take-Two Interactive | `take2games.com` | `candidate` | 1 | read 2 page(s) on take2games.com; no ATS fingerprint found |
| Ubisoft | `ubisoft.com` | `candidate` | 1 | read 8 page(s) on ubisoft.com; no ATS fingerprint found |
| Valve | `valvesoftware.com` | `candidate` | 1 | read 8 page(s) on valvesoftware.com; no ATS fingerprint found |
| Bungie | `bungie.net` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Discord | `discord.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| DeepL | `deepl.com` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Pydantic | `pydantic.dev` | `candidate` | 1 | read 5 page(s) on pydantic.dev; no ATS fingerprint found |
| Turso | `turso.tech` | `candidate` | 1 | read 4 page(s) on turso.tech; no ATS fingerprint found |
| Wasmer | `wasmer.io` | `candidate` | 1 | read 4 page(s) on wasmer.io; no ATS fingerprint found |
| Deno | `deno.com` | `empty` | 4 | verified employer domain -> endpoint (official_domain_link) |
| Oven (Bun) | `bun.sh` | `candidate` | 1 | provider names a different owner: Anthropic |
| Oxide Computer | `oxide.computer` | `candidate` | 1 | read 3 page(s) on oxide.computer; no ATS fingerprint found |
| Aikido Security | `aikido.dev` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Protect AI | `protectai.com` | `blocked` | 1 | challenge: workday challenge (status=429) |
| Lakera | `lakera.ai` | `empty` | 1 | verified employer domain -> endpoint (official_domain_link) |
| HiddenLayer | `hiddenlayer.com` | `candidate` | 1 | read 4 page(s) on hiddenlayer.com; no ATS fingerprint found |
| Ampere Computing | `amperecomputing.com` | `candidate` | 1 | read 1 page(s) on amperecomputing.com; no ATS fingerprint found |
| Celestial AI | `celestial.ai` | `candidate` | 1 | domain not verified |
| d-Matrix | `d-matrix.ai` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Enfabrica | `enfabrica.net` | `candidate` | 1 | read 1 page(s) on enfabrica.net; no ATS fingerprint found |

## Crawl health

| source | shards | crawl lag (min) | stale | shards in backoff |
|---|---:|---:|---|---:|
| `career-pages` | 64 | 6 | False | 0 |
| `icims` | 96 | 9 | False | 0 |
| `oracle` | 48 | 2 | False | 0 |
| `workday` | 48 | 12 | False | 0 |

- ingest runner heartbeat age: 0 min (healthy: True)

