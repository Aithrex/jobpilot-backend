# Technology employer coverage

**GENERATED FILE — do not edit by hand.**
Regenerate with:

```bash
PYTHONPATH=shared/python python scripts/employer_coverage_report.py --markdown
```

- measured at `2026-09-02T09:57:09.233996+00:00`
- source revision `aa76422`
- inventory watermark `2026-09-02T09:56:19.992000`
- posting-mix window: 30 days
- versions: `taxonomy=tech-tax-1.1.0`, `adjacency=tech-adjacency-1.0.0`, `employer_registry=employer-registry-1.0.0`, `career_source=career-source-1.0.0`, `employer_classifier=employer-tech-1.0.0`

## What is and is not claimed

This is not a claim to cover every technology company; no denominator
could support that sentence. It is a measured denominator — employers
we hold documentary evidence for — and a numerator that counts a career
source only when its OWNERSHIP is proven and its content validated.

| | value |
|---|---:|
| employers in the registry | 23,782 |
| **evidence-backed technology employers** | **2,622** |
| **validated official career sources** | **346** |
| **validated coverage** | **13.2%** |
| employers without validated coverage | 2,276 |
| employers yielding postings | 842 |
| employers yielding core-tech postings | 685 |
| active postings from them | 96,666 |
| core-tech postings | 15,506 (16.0%) |
| open employer merge reviews | 1,443 |

## Every employer is in exactly one state

No employer silently disappears: an employer with no career source at
all is `candidate`, and a provider failure moves a source to `blocked`,
`moved` or `dead` — never out of the denominator.

| state | employers |
|---|---:|
| `candidate` | 1,916 |
| `validated` | 322 |
| `content_validated` | 316 |
| `empty` | 24 |
| `ownership_verified` | 24 |
| `dead` | 14 |
| `blocked` | 6 |

## Coverage by cohort

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `public_tech` | 1,859 | 128 | 6.9% | 6,568 |
| `niche_startup` | 465 | 203 | 43.7% | 583 |
| `unknown` | 238 | 1 | 0.4% | 6,174 |
| `mnc` | 58 | 3 | 5.2% | 4,349 |
| `private_growth` | 33 | 12 | 36.4% | 797 |

## Coverage by category

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `other` | 2,473 | 336 | 13.6% | 15,506 |
| `software_saas` | 138 | 51 | 37.0% | 2,166 |
| `fintech` | 122 | 30 | 24.6% | 3,251 |
| `cloud_data` | 91 | 43 | 47.2% | 1,037 |
| `semiconductor` | 81 | 10 | 12.3% | 959 |
| `ai_ml` | 78 | 51 | 65.4% | 489 |
| `security` | 61 | 18 | 29.5% | 190 |
| `devtools` | 51 | 29 | 56.9% | 91 |
| `it_services` | 50 | 3 | 6.0% | 1,387 |
| `consumer` | 49 | 23 | 46.9% | 78 |
| `robotics` | 47 | 24 | 51.1% | 307 |
| `healthtech` | 39 | 17 | 43.6% | 224 |
| `gaming` | 26 | 6 | 23.1% | 45 |
| `aerospace_defense` | 15 | 7 | 46.7% | 354 |
| `energy` | 12 | 4 | 33.3% | 129 |
| `hardware` | 10 | 1 | 10.0% | 243 |
| `media` | 7 | 3 | 42.9% | 19 |
| `telecom` | 6 | 2 | 33.3% | 300 |
| `automotive` | 5 | 0 | 0.0% | 7 |
| `ecommerce` | 4 | 0 | 0.0% | 540 |

## Coverage by country

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `unknown` | 2,621 | 346 | 13.2% | 15,506 |
| `US` | 1 | 0 | 0.0% | 0 |

## Coverage by provider

| provider | career sources | ownership verified | validated |
|---|---:|---:|---:|
| `unknown` | 5,520 | 0 | 0 |
| `workday` | 618 | 116 | 6 |
| `greenhouse` | 588 | 263 | 263 |
| `icims` | 405 | 77 | 34 |
| `ashby` | 217 | 108 | 108 |
| `ukg` | 150 | 31 | 31 |
| `oracle_orc` | 145 | 36 | 0 |
| `paylocity` | 139 | 27 | 27 |
| `phenom` | 120 | 113 | 113 |
| `bamboohr` | 117 | 22 | 22 |
| `lever` | 113 | 35 | 35 |
| `smartrecruiters` | 70 | 24 | 24 |
| `workable` | 58 | 11 | 11 |
| `rippling` | 49 | 5 | 5 |
| `jazzhr` | 47 | 10 | 10 |
| `jobvite` | 39 | 11 | 11 |
| `eightfold` | 28 | 11 | 11 |
| `recruitee` | 23 | 3 | 3 |
| `taleo` | 22 | 1 | 0 |
| `cornerstone` | 22 | 5 | 5 |
| `successfactors` | 6 | 6 | 6 |
| `neogov` | 2 | 0 | 0 |

## The number this replaces

`company_ats_endpoints.status` records whether an endpoint ANSWERED.
That was previously reported as coverage. It is not coverage — it
cannot distinguish a company's own board from a stranger's board with
a similar slug. Both are shown so the gap is visible.

| endpoint status | count |
|---|---:|
| `validated` | 2,111 |
| `empty` | 297 |
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
| `candidate` | 76 |
| `validated` | 11 |
| `empty` | 3 |
| `content_validated` | 2 |
| `ownership_verified` | 2 |

| employer | domain | state | sources | detail |
|---|---|---|---:|---|
| Google | `google.com` | `candidate` | 0 | no career source discovered |
| Apple | `apple.com` | `candidate` | 1 | read 8 page(s) on apple.com; no ATS fingerprint found |
| Meta | `meta.com` | `candidate` | 0 | no career source discovered |
| Microsoft | `microsoft.com` | `candidate` | 0 | no career source discovered |
| IBM | `ibm.com` | `candidate` | 0 | no career source discovered |
| SAP | `sap.com` | `candidate` | 0 | no career source discovered |
| Salesforce | `salesforce.com` | `candidate` | 0 | no career source discovered |
| Tesla | `tesla.com` | `candidate` | 0 | no career source discovered |
| Atlassian | `atlassian.com` | `candidate` | 1 | read 8 page(s) on atlassian.com; no ATS fingerprint found |
| Shopify | `shopify.com` | `candidate` | 0 | no career source discovered |
| DigitalOcean | `digitalocean.com` | `candidate` | 0 | no career source discovered |
| Procore | `procore.com` | `candidate` | 0 | no career source discovered |
| monday.com | `monday.com` | `candidate` | 0 | no career source discovered |
| Fortinet | `fortinet.com` | `candidate` | 0 | no career source discovered |
| SentinelOne | `sentinelone.com` | `candidate` | 0 | no career source discovered |
| TSMC | `tsmc.com` | `candidate` | 0 | no career source discovered |
| Synopsys | `synopsys.com` | `candidate` | 0 | no career source discovered |
| Keysight | `keysight.com` | `candidate` | 0 | no career source discovered |
| Teradyne | `teradyne.com` | `candidate` | 0 | no career source discovered |
| Garmin | `garmin.com` | `candidate` | 0 | no career source discovered |
| Dell Technologies | `dell.com` | `candidate` | 0 | no career source discovered |
| Etsy | `etsy.com` | `candidate` | 0 | no career source discovered |
| Booking.com | `booking.com` | `candidate` | 1 | domain not verified |
| EPAM Systems | `epam.com` | `candidate` | 0 | no career source discovered |
| Rivian | `rivian.com` | `candidate` | 0 | no career source discovered |
| Snap | `snap.com` | `candidate` | 0 | no career source discovered |
| Klarna | `klarna.com` | `candidate` | 0 | no career source discovered |
| Whatnot | `whatnot.com` | `candidate` | 0 | no career source discovered |
| Amazon | `amazon.com` | `candidate` | 1 | read 8 page(s) on amazon.com; no ATS fingerprint found |
| Netflix | `netflix.com` | `candidate` | 0 | no career source discovered |
| Oracle | `oracle.com` | `candidate` | 0 | no career source discovered |
| Cisco | `cisco.com` | `empty` | 5 | verified employer domain -> endpoint (official_domain_link) |
| Adobe | `adobe.com` | `empty` | 5 | verified employer domain -> endpoint (official_domain_link) |
| Intel | `intel.com` | `content_validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Qualcomm | `qualcomm.com` | `candidate` | 0 | no career source discovered |
| Broadcom | `broadcom.com` | `candidate` | 1 | read 4 page(s) on broadcom.com; no ATS fingerprint found |
| VMware | `vmware.com` | `candidate` | 0 | no career source discovered |
| Uber | `uber.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Lyft | `lyft.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Airbnb | `airbnb.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Spotify | `spotify.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| HubSpot | `hubspot.com` | `candidate` | 0 | no career source discovered |
| Splunk | `splunk.com` | `candidate` | 0 | no career source discovered |
| Zoom | `zoom.com` | `candidate` | 0 | no career source discovered |
| Intuit | `intuit.com` | `candidate` | 0 | no career source discovered |
| Confluent | `confluent.io` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Arm | `arm.com` | `ownership_verified` | 2 | migrated to https://careers.arm.com/search-jobs |
| Seagate | `seagate.com` | `candidate` | 0 | no career source discovered |
| Lam Research | `lamresearch.com` | `empty` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Siemens | `siemens.com` | `candidate` | 0 | no career source discovered |
| Infosys | `infosys.com` | `candidate` | 0 | no career source discovered |
| Wipro | `wipro.com` | `candidate` | 0 | no career source discovered |
| Cognizant | `cognizant.com` | `candidate` | 1 | read 6 page(s) on cognizant.com; no ATS fingerprint found |
| Capgemini | `capgemini.com` | `candidate` | 1 | read 8 page(s) on capgemini.com; no ATS fingerprint found |
| Accenture | `accenture.com` | `ownership_verified` | 1 | no working site path (last=None) |
| Aurora Innovation | `aurora.tech` | `candidate` | 1 | read 5 page(s) on aurora.tech; no ATS fingerprint found |
| Goldman Sachs | `goldmansachs.com` | `candidate` | 0 | no career source discovered |
| Bloomberg | `bloomberg.com` | `candidate` | 1 | domain not verified |
| Nutanix | `nutanix.com` | `candidate` | 0 | no career source discovered |
| NetApp | `netapp.com` | `candidate` | 0 | no career source discovered |
| Akamai | `akamai.com` | `candidate` | 1 | read 8 page(s) on akamai.com; no ATS fingerprint found |
| X | `x.com` | `candidate` | 0 | no career source discovered |
| Blue Origin | `blueorigin.com` | `candidate` | 1 | domain not verified |
| Deel | `deel.com` | `candidate` | 0 | no career source discovered |
| Palantir | `palantir.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| F5 | `f5.com` | `candidate` | 0 | no career source discovered |
| Unity | `unity.com` | `content_validated` | 1 | no ownership evidence gathered |
| SpaceX | `spacex.com` | `candidate` | 0 | no career source discovered |
| Zoox | `zoox.com` | `candidate` | 0 | no career source discovered |
| Rippling | `rippling.com` | `candidate` | 0 | no career source discovered |
| Electronic Arts | `ea.com` | `candidate` | 0 | no career source discovered |
| Roblox | `roblox.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Riot Games | `riotgames.com` | `candidate` | 0 | no career source discovered |
| Epic Games | `epicgames.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Take-Two Interactive | `take2games.com` | `candidate` | 0 | no career source discovered |
| Ubisoft | `ubisoft.com` | `candidate` | 0 | no career source discovered |
| Valve | `valvesoftware.com` | `candidate` | 0 | no career source discovered |
| Bungie | `bungie.net` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Discord | `discord.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| DeepL | `deepl.com` | `candidate` | 0 | no career source discovered |
| Pydantic | `pydantic.dev` | `candidate` | 0 | no career source discovered |
| Turso | `turso.tech` | `candidate` | 0 | no career source discovered |
| Wasmer | `wasmer.io` | `candidate` | 0 | no career source discovered |
| Deno | `deno.com` | `candidate` | 0 | no career source discovered |
| Oven (Bun) | `bun.sh` | `candidate` | 1 | provider names a different owner: Anthropic |
| Oxide Computer | `oxide.computer` | `candidate` | 0 | no career source discovered |
| Aikido Security | `aikido.dev` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Protect AI | `protectai.com` | `candidate` | 0 | no career source discovered |
| Lakera | `lakera.ai` | `candidate` | 0 | no career source discovered |
| HiddenLayer | `hiddenlayer.com` | `candidate` | 0 | no career source discovered |
| Ampere Computing | `amperecomputing.com` | `candidate` | 1 | read 1 page(s) on amperecomputing.com; no ATS fingerprint found |
| Celestial AI | `celestial.ai` | `candidate` | 1 | domain not verified |
| d-Matrix | `d-matrix.ai` | `candidate` | 0 | no career source discovered |
| Enfabrica | `enfabrica.net` | `candidate` | 0 | no career source discovered |

## Crawl health

| source | shards | crawl lag (min) | stale | shards in backoff |
|---|---:|---:|---|---:|
| `career-pages` | 64 | 4 | False | 0 |
| `icims` | 96 | 7 | False | 0 |
| `oracle` | 48 | 0 | False | 0 |
| `workday` | 48 | 10 | False | 0 |

- ingest runner heartbeat age: 0 min (healthy: True)

