"""Which country is this posting in? Read-time, evidence-first, never a guess.

The defect, measured on live `job_postings` 2026-09-16, active +
`tech.tech_scope: core` (35,970 rows):

    locations.country_code known    10,363   (28.8%)
    locations.country_code == ""    25,579   (71.1%)
    no locations at all                765   ( 2.1%)

Only 3,780 were known-US. The remaining 71% are not "US by default" — the known
distribution (IN 1,873 · CA 1,038 · GB 481 · IL 282 · SG 161 · MX 107 …) proves
a large non-US population that we simply were not parsing. And because
`fit_score._score_location` reads `unknown` and unknown is EXCLUDED from the
average rather than penalised — the right rule for a genuinely unknown field —
a Spanish-language risk role in Mexico City scored 91 and was the #1 result for
an ASU new grad who needs H-1B sponsorship.

Why the field was empty is a PARSING gap, not missing data
-----------------------------------------------------------
`job_inventory_service.normalize_location` splits on commas positionally, so a
one-part string lands in `city` and the country aliases are never tried against
it. Measured over all 25,579 unresolved rows, the first location's raw string
falls into these shapes:

    us_state_position       5,400  21.1%   "Florham Park, NJ"  "US - Gaithersburg - MD"
    multi_location          4,870  19.0%   "10 Locations"  "6 Locations"
    country_name_in_city    3,543  13.9%   "Brazil"
    known_foreign_city      2,857  11.2%   "Sao Paulo"  "Bangkok MSG Headquarters"
    country_name_inside_raw 1,876   7.3%   "Mississauga Ontario Canada"
    remote_no_geo             950   3.7%   "United States of America : Remote"
    india_state               269   1.1%   "Bengaluru, KA"
    foreign_iso2_prefix       226   0.9%   "GB - Edinburgh"
    canada_province           150   0.6%   "Toronto, ON"

ORDER IS THE WHOLE DESIGN. A first draft tested the foreign-city gazetteer
before the US-state position and matched "FL - Ft Myers/**Naples** Division
Office: 28100 Bonita Grande Drive, Bonita Springs, FL 34135" as Italy. US proof
therefore runs BEFORE every foreign heuristic, and the loose tiers are gated on
`us_geo.has_foreign_marker` exactly as `us_geo.keep_us` gates its own.

What this module refuses to do
-------------------------------
* **It never defaults to US.** That is the same error as the current one,
  pointing the other way, and it would be far harder to see: a US candidate
  would stop noticing the Bangkok rows and start trusting a number that is
  wrong. `""` is a real answer and the feed treats it conservatively.
* **"10 Locations" stays unknown, permanently.** It is a Workday rollup
  placeholder naming no place at all. It is 19% of the unresolved set and
  resolving it would be inventing data.
* **It does not write.** Every caller resolves at READ time, so this is
  reversible by not calling it and needs no migration to start helping. The
  ingest-side fix (one delegation from `normalize_location`) plus a backfill is
  a separate, owned change — see `docs/specs/tech-feed-search-contract.md`.

It reuses `us_geo` rather than reimplementing it: that module already encodes
the hard-won collision rules (CA/DE/GA/IN/MN are US state codes AND country
codes; `IL` is Illinois far more often than Israel; "N Locations" is a rollup),
and a second opinion on those questions is how two surfaces come to disagree.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

RESOLVER_VERSION = "location-country-1.0.0"

#: Verdicts. `UNKNOWN` is a real answer and the most common one.
UNKNOWN = ""

#: The 88 foreign cities `us_geo` already certifies as having NO US namesake,
#: with the country each one is in. `us_geo` uses them to answer "is this
#: US?"; this map is what turns that into WHICH country, and it is restricted
#: to that vetted set on purpose — "Naples", "Birmingham", "Manchester",
#: "Athens" and "Paris" are all US cities too, and none of them is here.
_CITY_COUNTRY: Dict[str, str] = {
    "abu dhabi": "AE", "dubai": "AE", "ahmedabad": "IN", "bandung": "ID",
    "bangalore": "IN", "bangkok": "TH", "beijing": "CN",
    "belo horizonte": "BR", "bengaluru": "IN", "bhubaneswar": "IN",
    "brasilia": "BR", "buenos aires": "AR", "busan": "KR", "campinas": "BR",
    "cape town": "ZA", "casablanca": "MA", "cebu": "PH", "chandigarh": "IN",
    "chengdu": "CN", "chennai": "IN", "cochin": "IN", "coimbatore": "IN",
    "colombo": "LK", "dhaka": "BD", "doha": "QA", "gandhinagar": "IN",
    "guadalajara": "MX", "guangzhou": "CN", "gurgaon": "IN", "gurugram": "IN",
    "haifa": "IL", "hangzhou": "CN", "hanoi": "VN", "ho chi minh": "VN",
    "hyderabad": "IN", "indore": "IN", "islamabad": "PK", "jaipur": "IN",
    "jakarta": "ID", "johannesburg": "ZA", "karachi": "PK",
    "kathmandu": "NP", "kochi": "IN", "kolkata": "IN", "kuala lumpur": "MY",
    "kuwait city": "KW", "lahore": "PK", "makati": "PH", "manama": "BH",
    "medellin": "CO", "mexico city": "MX", "monterrey": "MX", "mumbai": "IN",
    "muscat": "OM", "mysore": "IN", "mysuru": "IN", "nagoya": "JP",
    "nagpur": "IN", "navi mumbai": "IN", "noida": "IN", "pasig": "PH",
    "penang": "MY", "porto alegre": "BR", "pretoria": "ZA", "pune": "IN",
    "queretaro": "MX", "quezon city": "PH", "rio de janeiro": "BR",
    "riyadh": "SA", "sao paulo": "BR", "secunderabad": "IN", "seoul": "KR",
    "shanghai": "CN", "shenzhen": "CN", "suzhou": "CN", "taguig": "PH",
    "taipei": "TW", "tel aviv": "IL", "thane": "IN",
    "thiruvananthapuram": "IN", "trivandrum": "IN", "tunis": "TN",
    "vadodara": "IN", "visakhapatnam": "IN", "vizag": "IN", "wuhan": "CN",
    "xian": "CN", "yokohama": "JP",
    # Local-language / abbreviated forms the boards actually print. `CDMX` is
    # what Santander stamps on Mexico City roles and it was the string in the
    # report that opened this work.
    "cdmx": "MX", "ciudad de mexico": "MX", "distrito federal": "MX",
    "bengaluru urban": "IN", "greater noida": "IN", "new delhi": "IN",
    "tel aviv yafo": "IL", "bengaluru rural": "IN",
    # European hubs with NO US namesake. Deliberately conservative: Berlin,
    # Hamburg, Madrid, Amsterdam, Dublin, Geneva, Athens, Naples, Manchester,
    # Stuttgart, Oslo, Valencia and Edinburgh are every one of them ALSO US
    # towns, and none of them is here. `DE - Darmstadt - ESOC` is the row that
    # forced this: it is ESA's operations centre and it read as Delaware.
    "darmstadt": "DE", "munchen": "DE", "muenchen": "DE", "dusseldorf": "DE",
    "duesseldorf": "DE", "monchengladbach": "DE", "wolfsburg": "DE",
    "milano": "IT", "torino": "IT", "bologna": "IT", "firenze": "IT",
    "napoli": "IT", "genova": "IT",
    "barcelona": "ES", "bilbao": "ES", "sevilla": "ES", "zaragoza": "ES",
    "lisboa": "PT", "braga": "PT",
    "utrecht": "NL", "eindhoven": "NL", "rotterdam": "NL",
    "antwerp": "BE", "antwerpen": "BE", "leuven": "BE",
    "zurich": "CH", "zuerich": "CH", "lausanne": "CH", "basel": "CH",
    "wien": "AT", "graz": "AT", "linz": "AT",
    "kobenhavn": "DK", "aarhus": "DK", "goteborg": "SE", "gothenburg": "SE",
    "malmo": "SE", "helsinki": "FI", "espoo": "FI", "tampere": "FI",
    "trondheim": "NO", "bergen": "NO",
    "warszawa": "PL", "krakow": "PL", "wroclaw": "PL", "gdansk": "PL",
    "poznan": "PL", "katowice": "PL", "lodz": "PL",
    "praha": "CZ", "brno": "CZ", "ostrava": "CZ",
    "budapest": "HU", "bucuresti": "RO", "cluj napoca": "RO",
    "timisoara": "RO", "bratislava": "SK", "ljubljana": "SI",
    "zagreb": "HR", "tallinn": "EE", "riga": "LV", "vilnius": "LT",
    "reykjavik": "IS", "thessaloniki": "GR",
    "istanbul": "TR", "ankara": "TR", "izmir": "TR",
    "kyiv": "UA", "kiev": "UA", "lviv": "UA",
    "montreal": "CA", "mississauga": "CA", "scarborough": "CA",
    "etobicoke": "CA", "markham": "CA", "brampton": "CA",
    "saskatoon": "CA", "winnipeg": "CA",
}

#: Canadian province and territory NAMES, which must NOT lead the string.
#:
#: "Toronto, Ontario" is Canada and "Ontario, California" is a city of 175,000
#: in San Bernardino County with its own international airport. The province
#: name follows its city; a leading "Ontario" IS the city. Measured: without
#: the `(?<=[,\s])` anchor this rule put Ontario, CA in Canada.
_CA_PROVINCE_NAME_RE = re.compile(
    r"(?<=[,\s])(ontario|quebec|british columbia|alberta|manitoba|"
    r"saskatchewan|nova scotia|new brunswick|newfoundland|"
    r"prince edward island|yukon|nunavut|northwest territories)(?![a-z])",
    re.IGNORECASE)

#: Language and postal markers that are decisive about a country even when the
#: string carries a leading token that collides with a US state code.
#:
#: The row that forced this: `DE-CELLE-BAKER-HUGHES-STRASSE 1` was resolved as
#: the UNITED STATES, because `us_geo` deliberately excludes `DE` from its
#: foreign ISO-2 prefixes (it is Delaware far more often) and the loose
#: state-position tier then read the leading `DE`. "STRASSE" is not a Delaware
#: word. This tier runs BEFORE the loose US tier for exactly that reason.
#:
#: `strasse` is read as DE. Austria and Switzerland use it too, and that is a
#: real and accepted residual: the alternative on this row was the United
#: States, which is wrong by a continent rather than by a border.
_FOREIGN_ADDRESS_MARKERS: Tuple[Tuple[str, str], ...] = (
    (r"stra(?:ss|\u00df)e", "DE"), (r"\bgmbh\b", "DE"), (r"\bplatz\b", "DE"),
    (r"\bs\.?a\.?r\.?l\b", "FR"), (r"\brue\b", "FR"), (r"\bcedex\b", "FR"),
    (r"\bb\.?v\.?\b", "NL"), (r"\bstraat\b", "NL"),
    (r"\bs\.?p\.?a\.?\b", "IT"), (r"\bvia\s+[a-z]", "IT"),
    (r"\bpvt\.?\s*ltd\b", "IN"), (r"\bsez\b", "IN"),
    (r"\bul\.\s", "PL"), (r"\bsp\.\s*z\s*o\.?o\b", "PL"),
)
_FOREIGN_ADDRESS_RE = tuple(
    (re.compile(pat, re.IGNORECASE), code)
    for pat, code in _FOREIGN_ADDRESS_MARKERS)


#: ISO-3 country prefixes. Three letters collide with no US state code, so
#: unlike the ISO-2 form this is safe to read positionally. `ITA - MI -
#: CERNUSCO SUL NAVIGLIO` was resolved as MICHIGAN without it.
_ISO3_COUNTRY: Dict[str, str] = {
    "usa": "US", "can": "CA", "mex": "MX", "bra": "BR", "arg": "AR",
    "gbr": "GB", "irl": "IE", "fra": "FR", "deu": "DE", "ger": "DE",
    "ita": "IT", "esp": "ES", "prt": "PT", "nld": "NL", "bel": "BE",
    "che": "CH", "aut": "AT", "swe": "SE", "nor": "NO", "dnk": "DK",
    "fin": "FI", "pol": "PL", "cze": "CZ", "hun": "HU", "rou": "RO",
    "bgr": "BG", "grc": "GR", "tur": "TR", "isr": "IL", "are": "AE",
    "sau": "SA", "ind": "IN", "chn": "CN", "jpn": "JP", "kor": "KR",
    "sgp": "SG", "mys": "MY", "tha": "TH", "vnm": "VN", "phl": "PH",
    "idn": "ID", "aus": "AU", "nzl": "NZ", "zaf": "ZA", "egy": "EG",
    "nga": "NG", "ken": "KE", "col": "CO", "chl": "CL", "per": "PE",
    "ukr": "UA", "pak": "PK", "bgd": "BD", "lka": "LK", "twn": "TW",
}
_ISO3_PREFIX_RE = re.compile(
    r"^\s*(" + "|".join(sorted(_ISO3_COUNTRY)) + r")\s*[-,/|>]\s*\S",
    re.IGNORECASE)


def _iso3_prefix(text: str) -> str:
    """ISO-2 from a leading ISO-3 country code ("ITA - MI - ..."), else ""."""
    m = _ISO3_PREFIX_RE.match(str(text or ""))
    return _ISO3_COUNTRY.get(m.group(1).lower(), UNKNOWN) if m else UNKNOWN


def _foreign_address_marker(text: str) -> str:
    """ISO-2 from a decisive language/postal marker in the address, else ""."""
    body = str(text or "")
    for rx, code in _FOREIGN_ADDRESS_RE:
        if rx.search(body):
            return code
    return UNKNOWN

#: Indian state / UT codes seen in the `region` position ("Bengaluru, KA").
#: `GA` (Goa) and `OR` (Odisha) are DELIBERATELY ABSENT — they are Georgia and
#: Oregon far more often, and this tier runs after US proof but a collision
#: that common is not worth 20 rows.
_IN_STATE_CODES = frozenset({
    "ka", "mh", "tn", "dl", "ts", "up", "wb", "gj", "hr", "pb", "kl",
    "rj", "ap", "mp", "as", "jh", "ct", "hp", "uk", "br",
})

#: Canadian province / territory codes in the `region` position.
_CA_PROVINCE_CODES = frozenset({
    "on", "bc", "ab", "qc", "mb", "sk", "ns", "nb", "nl", "pe", "nt",
    "yt", "nu",
})

#: Country names too ambiguous to accept from a bare substring scan. Every one
#: is also a US state, a US city, or a common given name.
#:
#:   georgia  -> the US state, 10x more often than the country
#:   india    -> "Indiana" is caught by folding, but "India St" is not
#:   jordan   -> a name and a US city (Jordan, MN)
#:   chad     -> a given name
#:   mexico   -> Mexico, MO and New Mexico
#:   turkey   -> Turkey Creek, and the bird
#:   lebanon  -> Lebanon, PA / OH / TN / NH — five US towns
_AMBIGUOUS_COUNTRY_NAMES = frozenset({
    "georgia", "india", "jordan", "chad", "mexico", "turkey", "lebanon",
    "guinea", "niger", "malta", "canada",
})

_WORD_RE = re.compile(r"[A-Za-z]+")

#: ISO-2 codes `us_geo._FOREIGN_ISO2` permits as a leading prefix, as a map so
#: the prefix yields the actual country rather than only "not US".
def _foreign_iso2_prefix(text: str) -> str:
    from services import us_geo
    m = us_geo._FOREIGN_ISO_PREFIX_RE.match(str(text or ""))
    if not m:
        return UNKNOWN
    return str(m.group(1)).upper()


def _fold(value: Any) -> str:
    from services.job_inventory_service import _fold as fold
    return fold(value)


def _country_of_name(value: Any) -> str:
    """ISO-2 for a string that IS a country name, else ""."""
    from services.job_inventory_service import normalize_country
    return normalize_country(value)


def _country_of_name_strict(value: Any) -> str:
    """`_country_of_name`, but a BARE two-letter token is never a country here.

    `job_inventory_service._alias` registers every ISO-2 code as its own alias,
    so `normalize_country("CO")` is Colombia and `normalize_country("CA")` is
    Canada. In the `city`/`region` position of a US address they are Colorado
    and California, and accepting them put **"Denver, CO, 80223" in Colombia**
    and "San Francisco, CA, New York, NY, …" in Canada — measured, 36 live
    rows, on the first run of this resolver.

    A spelled-out country name carries its own evidence; a two-letter token in
    a positional field carries none, and the positional tiers below
    (`us_state_in_region`, `canada_province`, `india_state`) are where a code
    is read, in an order that resolves the collision deliberately.
    """
    text = str(value or "").strip()
    if len(re.sub(r"[^A-Za-z]", "", text)) <= 2:
        return UNKNOWN
    return _country_of_name(text)


def _scan_country_names(text: str) -> str:
    """The country named INSIDE a free-text location, or "".

    Word n-grams, longest first, folded through the ONE alias table
    (`job_inventory_service._COUNTRY_ALIASES`) so there is no second country
    vocabulary to drift. Runs only AFTER US proof, and skips
    `_AMBIGUOUS_COUNTRY_NAMES`, because "Mississauga Ontario Canada" and
    "Jersey City New Jersey United States" are the shapes it is for and
    "Atlanta, Georgia" is the shape it must not answer.
    """
    words = _WORD_RE.findall(str(text or ""))
    if not words:
        return UNKNOWN
    for size in (4, 3, 2, 1):
        for i in range(len(words) - size + 1):
            gram = " ".join(words[i:i + size])
            if size == 1 and (gram.lower() in _AMBIGUOUS_COUNTRY_NAMES
                              or len(gram) <= 2):
                continue
            code = _country_of_name(gram)
            if code:
                return code
    return UNKNOWN


def _known_foreign_city(text: str) -> str:
    """ISO-2 when the string names one of the 88 vetted no-namesake cities."""
    folded = " ".join(_WORD_RE.findall(str(text or ""))).lower()
    if not folded:
        return UNKNOWN
    for city, code in _CITY_COUNTRY.items():
        if re.search(r"(?<![a-z])" + re.escape(city) + r"(?![a-z])", folded):
            return code
    return UNKNOWN


def is_multi_location(text: Any) -> bool:
    """"10 Locations" / "Multiple Locations" — a rollup naming no place.

    19.0% of the unresolved set. It stays UNKNOWN permanently: there is no
    country in the string to find, and inventing one is the failure this whole
    module exists to avoid.
    """
    from services import us_geo
    return bool(us_geo._MULTI_LOC_RE.search(str(text or "")))


def resolve(loc: Any, *, title: Any = "") -> Tuple[str, str]:
    """`(iso2, basis)` for ONE stored location entry. `("", basis)` is normal.

    `basis` names the rule that answered, so every resolution is auditable and
    a bad rule can be measured rather than argued about.
    """
    from services import us_geo

    if isinstance(loc, dict):
        raw = str(loc.get("raw") or "")
        city = str(loc.get("city") or "")
        region = str(loc.get("region") or "")
        country = str(loc.get("country") or "")
        stored = str(loc.get("country_code") or "").strip().upper()
    else:
        raw, city, region, country, stored = str(loc or ""), "", "", "", ""

    # 1. Already known. Never second-guessed - the ATS's own country field and
    #    a completed normalisation both land here.
    if stored:
        return stored, "stored"
    code = _country_of_name(country)
    if code:
        return code, "country_field"

    # 2. A rollup placeholder names no place. Stop before any heuristic can
    #    invent one from the digits.
    if is_multi_location(raw):
        return UNKNOWN, "multi_location"

    # 3. A whole field that IS a country name. This is the single largest
    #    repairable bucket (3,543 rows, "Brazil" filed as a city) and it exists
    #    purely because the positional parser puts a one-part string in `city`.
    for value, name in ((city, "country_name_in_city"),
                        (region, "country_name_in_region"),
                        (raw, "country_name_is_raw")):
        code = _country_of_name_strict(value)
        if code:
            return code, name

    # 4. An explicit foreign ISO-2 prefix ("GB - Edinburgh"). `us_geo` already
    #    excludes every code that collides with a US state.
    code = _foreign_iso2_prefix(raw)
    if code:
        return code, "foreign_iso2_prefix"

    # 5. US PROOF, and it runs before every foreign heuristic. A US state in
    #    the region position is unambiguous; the looser positional and
    #    name-based forms are gated on there being no foreign marker at all,
    #    the same gate `us_geo.keep_us` applies to its own loose tier.
    # `us_geo._STATE_CODES` is LOWERCASE. Comparing an upper-cased region
    # against it matched nothing, so this - the cheapest and most certain US
    # tier - never fired once across 35,970 live postings.
    if region.strip().lower() in set(us_geo._STATE_CODES):
        return "US", "us_state_in_region"
    foreign = us_geo.has_foreign_marker(raw)
    if not foreign:
        if us_geo.state_position_match(raw):
            return "US", "us_state_position"
        if us_geo._US_STATE_NAME_RE.search(raw):
            return "US", "us_state_name"
        if us_geo._US_WORD_RE.search(raw):
            return "US", "us_country_word"
        # A decisive foreign language/postal marker outranks the LOOSE state
        # position, which is the tier that reads a leading two-letter token and
        # therefore the tier that mistook `DE-CELLE-...-STRASSE 1` for Delaware.
        code = _iso3_prefix(raw)
        if code:
            return code, "iso3_prefix"
        code = _foreign_address_marker(raw)
        if code:
            return code, "foreign_address_marker"
        code = _known_foreign_city(raw) or _known_foreign_city(city)
        if code:
            return code, "known_foreign_city"
        if us_geo.state_position_match(raw, loose=True):
            return "US", "us_state_position_loose"

    # 6. Canada and India in the region position. Both are ordinary two-letter
    #    codes, so they run AFTER US proof by construction.
    if region.strip().lower() in _CA_PROVINCE_CODES \
            or us_geo._CA_PROVINCE_RE.search(raw) \
            or _CA_PROVINCE_NAME_RE.search(raw):
        return "CA", "canada_province"
    if region.strip().lower() in _IN_STATE_CODES:
        return "IN", "india_state"

    # 7. A country named inside free text, then the vetted foreign-city
    #    gazetteer. Last because both are substring scans.
    code = _scan_country_names(raw)
    if code:
        return code, "country_name_in_raw"
    code = _known_foreign_city(raw) or _known_foreign_city(city)
    if code:
        return code, "known_foreign_city"

    return UNKNOWN, "unresolved"


def resolve_posting(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Every country a posting's locations resolve to.

    Returns `{"codes": [...], "primary": str, "bases": [...],
    "multi_location": bool, "resolved": int, "total": int}`. `primary` is the
    FIRST resolved code, because the first location is the one the card shows.
    """
    locs = [l for l in (doc.get("locations") or []) if isinstance(l, dict)]
    title = str(doc.get("title_raw") or "")
    codes: List[str] = []
    bases: List[str] = []
    multi = False
    for loc in locs:
        code, basis = resolve(loc, title=title)
        bases.append(basis)
        if basis == "multi_location":
            multi = True
        if code and code not in codes:
            codes.append(code)
    return {
        "codes": codes,
        "primary": codes[0] if codes else UNKNOWN,
        "bases": bases,
        "multi_location": multi,
        "resolved": len(codes),
        "total": len(locs),
        "version": RESOLVER_VERSION,
    }


# ---------------------------------------------------------------------------
# The feed's work-location dimension
# ---------------------------------------------------------------------------
#: `work_location_verdict` values.
LOC_HOME = "home"        #: at least one location is in the candidate's country
LOC_FOREIGN = "foreign"  #: every resolved location is somewhere else
LOC_UNKNOWN = "unknown"  #: we could not read a country. 71% of the corpus.

#: Multiplicative `rank_score` penalties, mirroring `work_authorization`'s
#: shape so the two read consistently. A job in another country and an employer
#: that will not sponsor are DIFFERENT facts and get different badges and
#: different copy, but the same demote-not-delete treatment.
#:
#: There is no relegation tier here and that is deliberate. A stated
#: "no sponsorship" is the employer's own sentence; a country is OUR inference
#: from a string we have already proven we parse badly, and 71% of the corpus
#: is unreadable. A tier on top of that would be confident about the wrong
#: thing.
PENALTY_FOREIGN = 0.35
PENALTY_FOREIGN_UNDECLARED = 0.75

BADGE_FOREIGN = "foreign_location"
BADGE_LOCATION_UNKNOWN = "location_unknown"


def work_location_verdict(doc: Dict[str, Any], home_country: Any = "US"
                          ) -> Dict[str, Any]:
    """Is this posting in the candidate's country? Pure; no DB, no clock.

    `home_country` is an ISO-2. `unknown` is NEVER treated as foreign: 71% of
    live core postings cannot be read at all, and a hard rule on that would
    empty the feed rather than improve it.
    """
    home = str(home_country or "").strip().upper()
    resolved = resolve_posting(doc)
    codes = resolved["codes"]

    if not codes:
        verdict, penalty, badges, reasons = LOC_UNKNOWN, 1.0, [], []
    elif home and home in codes:
        verdict, penalty, badges, reasons = LOC_HOME, 1.0, [], []
    else:
        verdict = LOC_FOREIGN
        penalty = PENALTY_FOREIGN if home else 1.0
        badges = [BADGE_FOREIGN]
        where = ", ".join(codes[:3])
        reasons = [f"This role is based in {where}, not {home}"] if home else []
    return {
        "verdict": verdict,
        "country_codes": codes,
        "primary_country": resolved["primary"] or None,
        "multi_location": resolved["multi_location"],
        "penalty": penalty,
        "badges": badges,
        "reasons": reasons,
        "evaluated_against": home or None,
        "version": RESOLVER_VERSION,
    }
