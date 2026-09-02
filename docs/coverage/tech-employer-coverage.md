# Technology employer coverage

**GENERATED FILE — do not edit by hand.**
Regenerate with:

```bash
PYTHONPATH=shared/python python scripts/employer_coverage_report.py --markdown
```

- measured at `2026-09-02T13:30:59.482640+00:00`
- source revision `729d2cb-dirty`
- inventory watermark `2026-09-02T13:26:40.113000`
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
| **validated official career sources** | **868** |
| **validated coverage** | **33.1%** |
| employers without validated coverage | 1,756 |
| employers yielding postings | 845 |
| employers yielding core-tech postings | 689 |
| active postings from them | 98,626 |
| core-tech postings | 15,790 (16.0%) |
| open employer merge reviews | 1,443 |

## Every employer is in exactly one state

No employer silently disappears: an employer with no career source at
all is `candidate`, and a provider failure moves a source to `blocked`,
`moved` or `dead` — never out of the denominator.

| state | employers |
|---|---:|
| `candidate` | 1,508 |
| `validated` | 804 |
| `content_validated` | 236 |
| `empty` | 64 |
| `ownership_verified` | 9 |
| `blocked` | 3 |

## Coverage by cohort

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `public_tech` | 1,859 | 378 | 20.3% | 6,684 |
| `niche_startup` | 464 | 280 | 60.3% | 566 |
| `unknown` | 240 | 166 | 69.2% | 6,297 |
| `mnc` | 58 | 32 | 55.2% | 4,436 |
| `private_growth` | 34 | 23 | 67.7% | 827 |

## Coverage by category

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `other` | 2,475 | 835 | 33.7% | 15,790 |
| `software_saas` | 138 | 80 | 58.0% | 2,193 |
| `fintech` | 122 | 94 | 77.0% | 3,331 |
| `cloud_data` | 91 | 57 | 62.6% | 1,052 |
| `semiconductor` | 81 | 40 | 49.4% | 970 |
| `ai_ml` | 78 | 57 | 73.1% | 491 |
| `security` | 61 | 40 | 65.6% | 191 |
| `devtools` | 51 | 33 | 64.7% | 92 |
| `it_services` | 50 | 29 | 58.0% | 1,424 |
| `consumer` | 49 | 27 | 55.1% | 82 |
| `robotics` | 47 | 29 | 61.7% | 312 |
| `healthtech` | 39 | 33 | 84.6% | 227 |
| `gaming` | 26 | 10 | 38.5% | 45 |
| `aerospace_defense` | 15 | 13 | 86.7% | 355 |
| `energy` | 12 | 12 | 100.0% | 129 |
| `hardware` | 10 | 2 | 20.0% | 243 |
| `media` | 7 | 6 | 85.7% | 19 |
| `telecom` | 6 | 6 | 100.0% | 300 |
| `automotive` | 5 | 1 | 20.0% | 7 |
| `ecommerce` | 4 | 1 | 25.0% | 552 |

## Coverage by country

| | employers | validated | % | core-tech postings |
|---|---:|---:|---:|---:|
| `unknown` | 2,623 | 868 | 33.1% | 15,790 |
| `US` | 1 | 0 | 0.0% | 0 |

## Coverage by provider

| provider | career sources | ownership verified | validated |
|---|---:|---:|---:|
| `unknown` | 6,514 | 0 | 0 |
| `workday` | 1,509 | 989 | 895 |
| `greenhouse` | 994 | 566 | 566 |
| `icims` | 450 | 118 | 57 |
| `ashby` | 369 | 214 | 214 |
| `phenom` | 205 | 194 | 194 |
| `lever` | 187 | 99 | 99 |
| `oracle_orc` | 164 | 31 | 4 |
| `ukg` | 160 | 38 | 38 |
| `paylocity` | 149 | 36 | 36 |
| `smartrecruiters` | 138 | 89 | 89 |
| `bamboohr` | 127 | 32 | 32 |
| `workable` | 98 | 37 | 37 |
| `rippling` | 58 | 13 | 13 |
| `jobvite` | 51 | 23 | 23 |
| `jazzhr` | 51 | 13 | 13 |
| `recruitee` | 48 | 15 | 15 |
| `eightfold` | 39 | 22 | 22 |
| `taleo` | 26 | 2 | 0 |
| `cornerstone` | 26 | 7 | 7 |
| `successfactors` | 13 | 13 | 13 |
| `neogov` | 12 | 10 | 10 |
| `amazon` | 1 | 1 | 1 |
| `radancy` | 1 | 1 | 1 |

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
| `candidate` | 58 |
| `validated` | 24 |
| `empty` | 5 |
| `content_validated` | 4 |
| `ownership_verified` | 3 |

| employer | domain | state | sources | detail |
|---|---|---|---:|---|
| Google | `google.com` | `candidate` | 1 | domain not verified |
| Apple | `apple.com` | `candidate` | 1 | read 8 page(s) on apple.com; no ATS fingerprint found |
| Meta | `meta.com` | `candidate` | 1 | read 3 page(s) on meta.com; no ATS fingerprint found |
| Microsoft | `microsoft.com` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
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
| Garmin | `garmin.com` | `ownership_verified` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Dell Technologies | `dell.com` | `candidate` | 1 | read 8 page(s) on dell.com; no ATS fingerprint found |
| Etsy | `etsy.com` | `candidate` | 1 | domain not verified |
| Booking.com | `booking.com` | `candidate` | 1 | domain not verified |
| EPAM Systems | `epam.com` | `candidate` | 1 | domain not verified |
| Rivian | `rivian.com` | `ownership_verified` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Snap | `snap.com` | `candidate` | 1 | read 8 page(s) on snap.com; no ATS fingerprint found |
| Klarna | `klarna.com` | `candidate` | 1 | read 8 page(s) on klarna.com; no ATS fingerprint found |
| Whatnot | `whatnot.com` | `candidate` | 1 | domain not verified |
| Amazon | `amazon.com` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| Netflix | `netflix.com` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| Oracle | `oracle.com` | `content_validated` | 2 | one corroborating signal (ats_provider); the rule needs two independent classes |
| Cisco | `cisco.com` | `validated` | 6 | two independent corroborating signals: ats_provider, curated_registry |
| Adobe | `adobe.com` | `validated` | 5 | two independent corroborating signals: ats_provider, curated_registry |
| Intel | `intel.com` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Qualcomm | `qualcomm.com` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| Broadcom | `broadcom.com` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| VMware | `vmware.com` | `candidate` | 1 | read 3 page(s) on vmware.com; no ATS fingerprint found |
| Uber | `uber.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Lyft | `lyft.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Airbnb | `airbnb.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Spotify | `spotify.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| HubSpot | `hubspot.com` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| Splunk | `splunk.com` | `empty` | 5 | verified employer domain -> endpoint (official_domain_link) |
| Zoom | `zoom.com` | `candidate` | 1 | read 3 page(s) on zoom.com; no ATS fingerprint found |
| Intuit | `intuit.com` | `candidate` | 1 | read 8 page(s) on intuit.com; no ATS fingerprint found |
| Confluent | `confluent.io` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Arm | `arm.com` | `ownership_verified` | 2 | verified employer domain -> endpoint (official_domain_link) |
| Seagate | `seagate.com` | `candidate` | 1 | read 8 page(s) on seagate.com; no ATS fingerprint found |
| Lam Research | `lamresearch.com` | `empty` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Siemens | `siemens.com` | `candidate` | 1 | read 8 page(s) on siemens.com; no ATS fingerprint found |
| Infosys | `infosys.com` | `candidate` | 1 | read 8 page(s) on infosys.com; no ATS fingerprint found |
| Wipro | `wipro.com` | `candidate` | 1 | read 8 page(s) on wipro.com; no ATS fingerprint found |
| Cognizant | `cognizant.com` | `candidate` | 1 | read 6 page(s) on cognizant.com; no ATS fingerprint found |
| Capgemini | `capgemini.com` | `candidate` | 1 | read 8 page(s) on capgemini.com; no ATS fingerprint found |
| Accenture | `accenture.com` | `validated` | 1 | two independent corroborating signals: ats_provider, curated_registry |
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
| F5 | `f5.com` | `content_validated` | 2 | no ownership evidence gathered |
| Unity | `unity.com` | `content_validated` | 1 | no ownership evidence gathered |
| SpaceX | `spacex.com` | `candidate` | 2 | read 8 page(s) on spacex.com; no ATS fingerprint found |
| Zoox | `zoox.com` | `validated` | 2 | two independent corroborating signals: ats_provider, curated_registry |
| Rippling | `rippling.com` | `content_validated` | 2 | one corroborating signal (ats_provider); the rule needs two independent classes |
| Electronic Arts | `ea.com` | `candidate` | 1 | read 8 page(s) on ea.com; no ATS fingerprint found |
| Roblox | `roblox.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Riot Games | `riotgames.com` | `candidate` | 1 | read 8 page(s) on riotgames.com; no ATS fingerprint found |
| Epic Games | `epicgames.com` | `validated` | 1 | one corroborating signal (curated_registry); the rule needs two independent classes |
| Take-Two Interactive | `take2games.com` | `candidate` | 1 | read 7 page(s) on take2games.com; no ATS fingerprint found |
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
| Protect AI | `protectai.com` | `candidate` | 1 | no ownership evidence gathered |
| Lakera | `lakera.ai` | `empty` | 1 | verified employer domain -> endpoint (official_domain_link) |
| HiddenLayer | `hiddenlayer.com` | `candidate` | 1 | read 4 page(s) on hiddenlayer.com; no ATS fingerprint found |
| Ampere Computing | `amperecomputing.com` | `candidate` | 1 | read 1 page(s) on amperecomputing.com; no ATS fingerprint found |
| Celestial AI | `celestial.ai` | `candidate` | 1 | domain not verified |
| d-Matrix | `d-matrix.ai` | `validated` | 1 | verified employer domain -> endpoint (official_domain_link) |
| Enfabrica | `enfabrica.net` | `candidate` | 1 | read 1 page(s) on enfabrica.net; no ATS fingerprint found |

## Crawl health

| source | shards | crawl lag (min) | stale | shards in backoff |
|---|---:|---:|---|---:|
| `career-pages` | 64 | 8 | False | 0 |
| `icims` | 96 | 11 | False | 0 |
| `oracle` | 48 | 4 | False | 0 |
| `workday` | 48 | 13 | False | 0 |

- ingest runner heartbeat age: 0 min (healthy: True)

