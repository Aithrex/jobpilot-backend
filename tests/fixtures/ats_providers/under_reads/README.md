# Live response fixtures

- `icims_emory.html`: 2026-11-05T22:47:51.759316Z, GET https://staff-emory.icims.com/jobs/search?ss=1&in_iframe=1. Rows trimmed; reported totals unchanged.
- `taleo_aarcorp.json`: 2026-11-05T22:48:38.951321Z, POST https://aarcorp.taleo.net/careersection/rest/jobboard/searchjobs?lang=en&portal=101430233. Rows trimmed; reported totals unchanged.
- `phenom_baesystems.json`: 2026-11-05T22:48:47.604209Z, POST https://jobs.baesystems.com/widgets. Rows trimmed; reported totals unchanged.
- `workable_trexquant.json`: 2026-11-05T22:49:04.188479Z, GET https://apply.workable.com/api/v1/widget/accounts/trexquant?details=true. Rows trimmed; reported totals unchanged.

Responses captured by the parent during serialized public probes; no database access.

- `successfactors_pseg_detail.html`: 2026-11-05T23:01:08.908778Z, GET https://jobs.pseg.com/job/Newark-10078-IT-Solution-Architect-Principal-II-NJ-07101/1414110400/. Visible location and first description paragraph retained; live full detail supplied 9898 description characters.
- `successfactors_psegliny_detail.html`: 2026-11-05T23:01:12.331158Z, GET https://jobs.psegliny.com/LI/job/Bethpage-Sr-Network-Engineer-NY-11804/1437694800/. Visible location and first description paragraph retained; live full detail supplied 9353 description characters.
# Netflix Eightfold classic

`eightfold_netflix.json` retains three positions and the published `count=231`
from `https://explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com&query=&location=United+States&start=0&num=10`,
captured at `2026-11-05T23:07:03.688915Z`. The response supplies no posting dates.
