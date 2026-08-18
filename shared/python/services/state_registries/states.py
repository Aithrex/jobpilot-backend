"""The 51-jurisdiction table.

Every US state plus DC appears here exactly once. Native harvestable sources
(Socrata, Sunbiz bulk, bizfile JSON) are used when we have a verified
endpoint and column map. Every other jurisdiction gets a FederalProxyAdapter
so G5 reports coverage that is actually reachable — operating employers via
federal signals — rather than a `search` stub that can never enumerate.

Native sources were verified by fetching `$limit=1` and reading column names
off the response (2026-08-17).
"""
from __future__ import annotations

from typing import Dict, List, Set

from .base import register
from .bizfile import BizfileAdapter
from .federal import FederalProxyAdapter
from .socrata import SocrataAdapter
from .sunbiz import SunbizDailyAdapter

_ALL_STATES: List[tuple] = [
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"),
    ("CA", "California"), ("CO", "Colorado"), ("CT", "Connecticut"),
    ("DE", "Delaware"), ("DC", "District of Columbia"), ("FL", "Florida"),
    ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"), ("IL", "Illinois"),
    ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"), ("KY", "Kentucky"),
    ("LA", "Louisiana"), ("ME", "Maine"), ("MD", "Maryland"),
    ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"),
    ("MS", "Mississippi"), ("MO", "Missouri"), ("MT", "Montana"),
    ("NE", "Nebraska"), ("NV", "Nevada"), ("NH", "New Hampshire"),
    ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"),
    ("OK", "Oklahoma"), ("OR", "Oregon"), ("PA", "Pennsylvania"),
    ("RI", "Rhode Island"), ("SC", "South Carolina"), ("SD", "South Dakota"),
    ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"), ("VT", "Vermont"),
    ("VA", "Virginia"), ("WA", "Washington"), ("WV", "West Virginia"),
    ("WI", "Wisconsin"), ("WY", "Wyoming"),
]

# Verified live. `record_estimate` is a measured count(*) where we have one.
_SOCRATA: List[Dict] = [
    dict(
        state="NY", name="New York",
        url="https://data.ny.gov/resource/n9v6-gdp6.json",
        portal_url="https://data.ny.gov/Economic-Development/Active-Corporations-Beginning-1800/n9v6-gdp6",
        record_estimate=4_266_351,
        fields={"name": "current_entity_name", "type": "entity_type",
                "registered": "initial_dos_filing_date",
                "city": "dos_process_city", "postal": "dos_process_zip",
                "registry_id": "dos_id"},
        notes="Active corporations only; address is often the DOS process agent.",
    ),
    dict(
        state="CO", name="Colorado",
        url="https://data.colorado.gov/resource/4ykn-tg5h.json",
        portal_url="https://data.colorado.gov/Business/Business-Entities-in-Colorado/4ykn-tg5h",
        record_estimate=3_095_631,
        fields={"name": "entityname", "type": "entitytype",
                "status": "entitystatus", "registered": "entityformdate",
                "city": "principalcity", "postal": "principalzipcode",
                "registry_id": "entityid"},
        where="upper(entitystatus)='GOOD STANDING'",
        notes="Filter Good Standing server-side; ~1M of 3.1M are operating.",
    ),
    dict(
        state="PA", name="Pennsylvania",
        url="https://data.pa.gov/resource/xvd7-5r2c.json",
        portal_url="https://data.pa.gov/Business/PA-Businesses/xvd7-5r2c",
        record_estimate=2_310_000,
        fields={"name": "business_name", "type": "typeofbusinessregistration",
                "registered": "creationdate", "city": "city", "postal": "zip",
                "registry_id": "filing_number"},
        notes="Officer-level rows; company_id_for(name,state) dedupes on upsert.",
    ),
    dict(
        state="OR", name="Oregon",
        url="https://data.oregon.gov/resource/tckn-sxa6.json",
        portal_url="https://data.oregon.gov/Business/Business-Registry/tckn-sxa6",
        record_estimate=1_560_000,
        fields={"name": "business_name", "type": "entity_type",
                "registered": "registry_date", "city": "city", "postal": "zip",
                "registry_id": "registry_number"},
        notes="One row per associated name; dedupe on registry_number at upsert.",
    ),
    dict(
        state="CT", name="Connecticut",
        url="https://data.ct.gov/resource/n7gp-d28j.json",
        portal_url="https://data.ct.gov/Business/Business-Registry/n7gp-d28j",
        record_estimate=1_280_000,
        fields={"name": "name", "status": "status",
                "registered": "date_registration",
                "registry_id": "accountnumber"},
        notes="Master table; 'Rejected' statuses dropped by is_probably_operating.",
    ),
]


def _build() -> None:
    native: Set[str] = set()
    for spec in _SOCRATA:
        register(SocrataAdapter(**spec))
        native.add(spec["state"])
    register(SunbizDailyAdapter())
    native.add("FL")
    register(BizfileAdapter(
        state="ID", name="Idaho", host="sosbiz.idaho.gov",
        notes="PCC bizfile JSON; term-sweep, not a full dump."))
    native.add("ID")
    register(BizfileAdapter(
        state="ND", name="North Dakota", host="firststop.sos.nd.gov",
        notes="PCC bizfile JSON; term-sweep, not a full dump."))
    native.add("ND")
    for code, name in _ALL_STATES:
        if code in native:
            continue
        register(FederalProxyAdapter(
            state=code, name=name,
            notes=(f"{name} SoS is search-only or paid; this adapter enumerates "
                   "operating employers via the federal seed + EDGAR/H-1B.")))


_build()
