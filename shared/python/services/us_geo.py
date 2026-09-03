"""THE US geo filter — one decision, used by every surface that claims "US only".

Why this module exists
----------------------
There were two independent implementations of the same rule:
``career_pages_service._keep_us`` and ``workday_jobs_service._geo_keep_us``.
They disagreed (the career-pages one had no bare-"US" tier and no Toronto
guard), and ``inventory_feed`` — which serves BOTH source tabs — called the
weaker of the two. A measurement over 6,000 live active postings found the
combined rule wrong in both directions:

  LEAKS IN (non-US served in a US-only slice), all caused by the two-letter
  state-CODE tier matching a substring that is not a state at all:
    "Hyderabad-Hi-Tec City"                 -> "Hi" read as Hawaii
    "Montreal - 1000 Rue De La Gauchetiere" -> "De" read as Delaware
    "CA - Toronto" / "CA-Toronto-York St"   -> "CA" read as California
    "BR - Rio de Janeiro"                   -> "de" read as Delaware
    "CA, Ontario"                           -> "CA" read as California

  DROPPED OUT (real US roles the user never sees):
    "US"                (185 rows) -> the US word tier only knew "USA"/"U.S."
    "San Jose" / "San Francisco" / "Boston" / "Boca Raton"
                                   -> bare major US city, no state token
    "Indianapolis, IN"   (26 rows) -> "in" is deliberately excluded from the
                                      state-code set (it would match
                                      "Hyderabad IN"), leaving nothing to
                                      rescue the row

The fix is not a longer blocklist. It is making the state-code tier
POSITIONAL: a two-letter state code is only a state when it sits where a state
sits — after a comma, as the final token, next to a ZIP, or adjacent to a US
country word. "Hi" inside "Hi-Tec" is in none of those positions, and neither
is the "CA" of "CA - Toronto" once a foreign city is present in the string.

Decision order (keep clearly-US or clearly-remote, DROP everything ambiguous)
----------------------------------------------------------------------------
0. Authoritative country code from the ATS payload wins outright.
1. Explicit foreign COUNTRY name, Canadian province, or a leading foreign
   ISO-2 country prefix ("BR - Rio de Janeiro") -> DROP.
2. US country word ("United States", "USA", bare "US", "U.S.") -> KEEP.
3. Spelled-out US state NAME -> KEEP ("Athens, Georgia", "Indiana").
4. Foreign hub CITY present -> DROP unless a state code sits in an
   UNAMBIGUOUS state position ("Berlin, CT" keeps, "Hyderabad-Hi-Tec" drops).
5. State code in any accepted state position -> KEEP ("VA - Reston, ...").
6. Known unambiguous large US CITY -> KEEP ("San Jose", "Boston").
7. Clear remote with no foreign marker -> KEEP.
8. Multi-location rollup ("5 Locations") -> KEPT only at the list stage, where
   detail enrichment can still resolve the country; dropped when serving.
9. Else (bare unknown city, campus name, empty) -> DROP.

The keep/drop CONTRACT is unchanged — ambiguous still drops. Only the evidence
each tier accepts got sharper.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional

__all__ = [
    "keep_us", "keep_us_row", "US_COUNTRY_OK",
    "has_foreign_marker", "state_position_match",
]

# Authoritative country values that mean "United States". Normalized (lower,
# stripped of dots) before lookup so "U.S.A." and "united-states" both land.
US_COUNTRY_OK = frozenset({
    "us", "usa", "united states", "united states of america", "america",
    "united states minor outlying islands", "u s", "u s a", "us of a",
    "puerto rico", "pr",
})

# ---------------------------------------------------------------------------
# Tier 1 — foreign COUNTRY / province / ISO prefix
# ---------------------------------------------------------------------------
_NON_US_COUNTRY_MARKERS = (
    "india", "canada", "united kingdom", "uk", "great britain", "england",
    "scotland", "wales", "northern ireland", "ireland", "poland",
    "philippines", "mexico", "brazil", "brasil", "cdmx", "china", "japan",
    "singapore", "turkiye",
    "australia", "germany", "france", "spain", "italy", "netherlands",
    "romania", "hungary", "czech", "czechia", "slovakia", "slovenia",
    "croatia", "serbia", "bulgaria", "ukraine", "estonia", "latvia",
    "lithuania", "israel", "egypt", "colombia", "argentina", "costa rica",
    "malaysia", "vietnam", "thailand", "indonesia", "switzerland", "sweden",
    "denmark", "norway", "finland", "belgium", "austria", "portugal",
    "greece", "turkey", "united arab emirates", "uae", "saudi arabia",
    "saudi", "qatar", "bahrain", "kuwait", "oman", "south africa", "kenya",
    "nigeria", "ghana", "morocco", "tunisia", "hong kong", "taiwan",
    "south korea", "korea", "new zealand", "chile", "peru", "ecuador",
    "uruguay", "paraguay", "bolivia", "venezuela", "guatemala",
    # "Panama City, FL" and "Jamaica Plain-MA" (Boston) are US rows; the bare
    # country name dropped both. The country still matches on its own
    # ("Panama City, Panama" hits the trailing occurrence).
    r"panama(?!\s*city)", r"jamaica(?!\s+plain)",
    "dominican republic", "sri lanka", "pakistan", "bangladesh",
    "nepal", "myanmar", "cambodia", "laos", "brunei", "luxembourg",
    "iceland", "malta", "cyprus", "russia", "kazakhstan", "uzbekistan",
    "armenia", "georgia country", "azerbaijan", "mongolia", "fiji",
    # Regional rollups that are never a US posting.
    "emea", "apac", "latam", "latin america", "south america",
    "asia pacific", "middle east",
)
_NON_US_COUNTRY_RE = re.compile(
    # "mexico" must not fire inside "New Mexico" — "Albuquerque, New Mexico,
    # United States" is a US posting and the plain \bmexico\b dropped it.
    # Same shape as the existing "india" / "Indiana" guard.
    r"(?<!new )\b(" + "|".join(
        # A handful of markers carry their own lookahead (see above); those
        # must reach the engine unescaped.
        m if "(?" in m else re.escape(m) for m in _NON_US_COUNTRY_MARKERS
    ) + r")\b",
    re.IGNORECASE,
)

# Canadian provinces, comma-anchored so "On-site" / "Ab initio" prose never
# matches — Canadian postings write "Toronto, ON" / "Vancouver, BC".
_CA_PROVINCE_RE = re.compile(
    r",\s*(on|bc|ab|qc|mb|sk|ns|nb|nl|pe|nt|yt|nu)\b", re.IGNORECASE)

# Workday and several career boards prefix the ISO-2 COUNTRY: "BR - Rio de
# Janeiro", "PL - Krakow", "JP - Tokyo". Only codes that are NOT also a US
# state code are listed — "CA"/"DE"/"GA"/"IN"/"MN" are excluded because the
# collision makes the prefix unreadable on its own; those rows are decided by
# the foreign-city and state-position tiers instead.
_FOREIGN_ISO2 = (
    "br", "gb", "jp", "cn", "kr", "sg", "ph", "my", "vn", "th", "af",
    "ae", "qa", "za", "ng", "eg", "tr", "gr", "pt", "cz", "ro", "hu",
    "bg", "hr", "rs", "ua", "at", "be", "ch", "se", "dk", "no", "fi",
    "nl", "es", "it", "fr", "pl", "au", "nz", "mx", "cl", "pe",
    # "il" (Israel) is deliberately ABSENT — it collides with Illinois, and
    # "IL, Peoria" / "IL-Chicago; 303 E Wacker Drive" are real US rows the
    # prefix rule dropped. Israeli rows are caught by the country name and by
    # "tel aviv"/"jerusalem"/"haifa" in the city tier.
    "tw", "hk", "pk", "bd", "lk", "ie", "kz", "uz", "np", "kh", "bn",
    "lu", "is", "cy", "ee", "lv", "lt", "sk", "si", "ba", "mk", "by",
    "ru", "ec", "uy", "py", "bo", "ve", "gt", "do", "jm", "tt", "zw",
    "gh", "tz", "ug", "et", "dz", "sn", "ci", "cm", "rw", "mu", "fj",
)
# Anchored at the start and followed by a separator: "BR - ...", "PL-...",
# "SG, ...". A bare "BR" somewhere mid-string is NOT enough.
_FOREIGN_ISO_PREFIX_RE = re.compile(
    r"^\s*(" + "|".join(_FOREIGN_ISO2) + r")\s*[-,/|]\s*\S", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Tier 2/3 — US country word and spelled-out state NAME
# ---------------------------------------------------------------------------
# Bare "US" is included here (it was the single largest false drop: 185 rows
# whose entire location string is "US"). It is safe at THIS position because
# tier 1 has already removed every string carrying a foreign country marker.
_US_WORD_RE = re.compile(
    r"(united states|\busa\b|\bus\b|\bstateside\b"
    # "U.S." / "U.S.A." — no trailing \b, because the string frequently ENDS
    # on the final dot and \b cannot fire after a non-word character.
    r"|\bu\.\s?s\.?(?:\s?a\.?)?"
    r"|remote[ ,-]+us\b)", re.IGNORECASE)

_US_STATE_NAMES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming", "district of columbia",
    "puerto rico",
)
_US_STATE_NAME_RE = re.compile(
    r"\b(" + "|".join(re.escape(m) for m in _US_STATE_NAMES) + r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Tier 4 — foreign hub CITY
# ---------------------------------------------------------------------------
_NON_US_CITY_MARKERS = (
    # India
    "pune", "bengaluru", "bangalore", "hyderabad", "chennai", "mumbai",
    "new delhi", "delhi", "noida", "gurgaon", "gurugram", "kolkata",
    "ahmedabad", "kochi", "cochin", "coimbatore", "trivandrum",
    "thiruvananthapuram", "indore", "nagpur", "vadodara", "mysore",
    "mysuru", "chandigarh", "jaipur", "bhubaneswar", "visakhapatnam",
    "vizag", "gandhinagar", "thane", "navi mumbai", "secunderabad",
    # Canada. "ontario" lives HERE rather than in the country tier because it
    # collides with two real US places ("Ontario, California", "Ontario, OH");
    # the city tier is the one that allows a state token to rescue the row,
    # which is exactly the behaviour those two need. A bare "CA, Ontario"
    # (Workday's spelling for Canada) has no rescuing state and drops.
    "toronto", "vancouver", "montreal", "mississauga", "ottawa", "calgary",
    "waterloo", "edmonton", "winnipeg", "halifax", "quebec city", "burnaby",
    "markham", "brampton", "richmond hill", "kitchener", "ontario",
    # UK / Ireland / Europe
    "london", "edinburgh", "glasgow", "belfast", "leeds", "manchester",
    "birmingham uk", "bristol", "cambridge uk", "dublin", "cork", "galway",
    "warsaw", "krakow", "wroclaw", "gdansk", "poznan", "bucharest",
    "budapest", "prague", "brno", "bratislava", "lisbon", "porto", "madrid",
    "barcelona", "valencia", "munich", "berlin", "hamburg", "frankfurt",
    "stuttgart", "cologne", "dusseldorf", "amsterdam", "rotterdam",
    "utrecht", "eindhoven", "zurich", "geneva", "basel", "stockholm",
    "gothenburg", "copenhagen", "oslo", "helsinki", "brussels", "antwerp",
    "milan", "rome", "turin", "naples", "vienna austria", "sofia", "belgrade",
    "zagreb", "ljubljana", "tallinn", "riga", "vilnius", "athens greece",
    "thessaloniki", "istanbul", "ankara", "kyiv", "kiev", "lviv", "minsk",
    "moscow", "saint petersburg",
    # MEA
    "tel aviv", "jerusalem", "haifa", "cairo", "nairobi", "lagos", "abuja",
    "accra", "dubai", "abu dhabi", "riyadh", "doha", "kuwait city",
    "manama", "muscat", "casablanca", "tunis", "johannesburg", "cape town",
    "pretoria", "durban",
    # APAC / LatAm
    "seoul", "busan", "taipei", "jakarta", "bandung", "kuala lumpur",
    "penang", "ho chi minh", "hanoi", "bangkok", "tokyo", "osaka", "kyoto",
    "yokohama", "nagoya", "sydney", "melbourne", "brisbane", "perth",
    "adelaide", "canberra", "auckland", "wellington", "christchurch",
    "shanghai", "beijing", "shenzhen", "guangzhou", "chengdu", "hangzhou",
    "suzhou", "wuhan", "xian", "makati", "manila", "taguig", "cebu",
    "quezon city", "pasig", "guadalajara", "monterrey", "mexico city",
    "queretaro", "sao paulo", "rio de janeiro", "brasilia", "belo horizonte",
    "porto alegre", "campinas", "buenos aires", "cordoba argentina",
    "santiago", "bogota", "medellin", "lima", "san jose costa rica",
    "montevideo", "quito", "panama city", "colombo", "karachi", "lahore",
    "islamabad", "dhaka", "kathmandu", "colombo",
)
_NON_US_CITY_RE = re.compile(
    r"\b(" + "|".join(re.escape(m) for m in _NON_US_CITY_MARKERS) + r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Tier 2a - foreign hub cities with NO US namesake
# ---------------------------------------------------------------------------
# The tier-2 rescue exists for real US namesakes: "Dublin, OH", "Paris, KY",
# "Vancouver, WA", "Melbourne, FL" are all US postings that a foreign city
# marker would otherwise drop, and STRICT positional state evidence is what
# saves them. Measured on the live inventory, that rescue works: of 17,208
# clearly-non-US active rows, the filter kept 248 - and 243 of those were
# genuine US towns my audit's own regex had flagged by name.
#
# The other five were a real leak, and all of one shape: a foreign hub whose
# SUBDIVISION code collides with a US state code, so the rescue fires on the
# city's own region.
#
#     "Chennai, TN"  -> TN is Tamil Nadu, read as Tennessee   (4 rows, JLL)
#     "Jakarta, ID"  -> ID is Indonesia,  read as Idaho       (1 row,  S&P Global)
#
# There is no "Chennai, Tennessee" and no "Jakarta, Idaho", so for these cities
# the rescue can only ever be wrong. Listing them here makes the marker a hard
# drop instead. Membership is deliberately conservative - a city stays on the
# rescue path unless it certainly has no US namesake - because a wrong entry
# here removes real US jobs, which is the worse error. "Delhi" is therefore
# absent (Delhi, NY and Delhi, CA), as are Berlin, Lima, Naples, Milan, Rome,
# Manila, Bogota, Panama City, Saint Petersburg, Wellington and Montevideo.
_NON_US_CITY_NO_NAMESAKE = frozenset({
    # India - the tech hubs. Their state codes TN, KA, MH, AP, TS, GJ, WB, OR
    # and UP overlap the US set at TN and OR.
    "pune", "bengaluru", "bangalore", "hyderabad", "chennai", "mumbai",
    "noida", "gurgaon", "gurugram", "kolkata", "ahmedabad", "kochi", "cochin",
    "coimbatore", "trivandrum", "thiruvananthapuram", "indore", "nagpur",
    "vadodara", "mysore", "mysuru", "chandigarh", "jaipur", "bhubaneswar",
    "visakhapatnam", "vizag", "gandhinagar", "thane", "navi mumbai",
    "secunderabad",
    # Greater China
    "shanghai", "beijing", "shenzhen", "guangzhou", "chengdu", "hangzhou",
    "suzhou", "wuhan", "xian", "taipei",
    # SEA / APAC. "ID" (Indonesia) collides with Idaho, "SG" and "MY" do not.
    "jakarta", "bandung", "kuala lumpur", "penang", "ho chi minh", "hanoi",
    "bangkok", "makati", "taguig", "cebu",
    "quezon city", "pasig", "seoul", "busan", "yokohama", "nagoya",
    # Gulf / MEA
    "dubai", "abu dhabi", "riyadh", "doha", "kuwait city", "manama", "muscat",
    "tel aviv", "haifa", "casablanca", "tunis", "johannesburg", "cape town",
    "pretoria",
    # LatAm
    "sao paulo", "rio de janeiro", "brasilia", "belo horizonte",
    "porto alegre", "campinas", "guadalajara", "monterrey", "queretaro",
    "mexico city", "buenos aires", "medellin",
    # South Asia
    "karachi", "lahore", "islamabad", "dhaka", "kathmandu", "colombo",
})
_NON_US_CITY_NO_NAMESAKE_RE = re.compile(
    r"\b(" + "|".join(re.escape(m) for m in sorted(_NON_US_CITY_NO_NAMESAKE))
    + r")\b", re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Tier 5 — POSITIONAL state code
# ---------------------------------------------------------------------------
# "in" stays out: "Hyderabad IN" would otherwise read as Indiana. Rows that
# genuinely say "Indianapolis, IN" are rescued by the US-city tier below.
_STATE_CODES = (
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo",
    "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or",
    "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi",
    "wy", "dc", "pr",
)
_SC = "|".join(_STATE_CODES)

# STRICT positions — a two-letter token here is a US state and nothing else.
# These are the ONLY evidence accepted to rescue a row that also names a
# foreign hub city ("Berlin, CT" keeps; "Hyderabad-Hi-Tec City" does not).
_STATE_POSITION_STRICT = (
    # "Austin, TX" · "Dublin, OH" · "Ontario, OH" · "Tempe, AZ 85281"
    re.compile(r",\s*(" + _SC + r")\b", re.IGNORECASE),
    # "Tempe AZ" — final token of the string.
    re.compile(r"\b(" + _SC + r")\s*$", re.IGNORECASE),
    # "Princeton - NJ - US" · "USA - IL (Remote)" — beside a US country word.
    re.compile(r"\b(?:us|usa|u\.s\.|united states)\b[\s,\-/|]+(" + _SC + r")\b",
               re.IGNORECASE),
    re.compile(r"\b(" + _SC + r")\b[\s,\-/|]+(?:us|usa|u\.s\.|united states)\b",
               re.IGNORECASE),
    # "Phoenix AZ 85004" — state code immediately before a ZIP.
    re.compile(r"\b(" + _SC + r")\s+\d{5}(?:-\d{4})?\b", re.IGNORECASE),
    # "Remote (TX)" · "Field - Remote, NY"
    re.compile(r"[(\[]\s*(" + _SC + r")\s*[)\]]", re.IGNORECASE),
)

# LOOSE positions — admissible ONLY after every foreign marker has been ruled
# out, because neither can distinguish "CA - Sacramento" from "CA - Toronto"
# on its own.
#
#   * leading prefix: "AZ - Phoenix", "IL, Peoria", "UT MAIN CAMPUS", the
#     mirror image of the foreign ISO-2 prefix;
#   * an UPPERCASE standalone code anywhere: "North Bethesda MD - Corporate
#     Headquarters", "Newport Beach CA-700", "Kernersville-NC 1045 Industrial
#     Park Dr", "WA Country". Case-sensitivity is what makes this safe — it is
#     the difference between the state code "LA" and the French article in
#     "Calvin Klein La Defense", between "DE" and "Cuajimalpa de Morelos", and
#     between "MT" and "Telstra Store Mt Gambier", all of which are real rows
#     the case-insensitive version leaked.
_STATE_POSITION_LEADING = re.compile(
    r"^\s*(" + _SC + r")\b[\s,\-/|]+\S", re.IGNORECASE)
_STATE_POSITION_UPPER = re.compile(
    r"(?<![A-Za-z0-9])(" + _SC.upper() + r")(?![A-Za-z0-9])")

# ---------------------------------------------------------------------------
# Tier 6 — unambiguous large US cities
# ---------------------------------------------------------------------------
# Only cities whose bare name is not a well-known foreign hub. "Vancouver",
# "Manchester", "Berlin", "Dublin", "Athens", "Paris", "Vienna", "Birmingham",
# "Cambridge", "Columbus" and friends are deliberately ABSENT — those rows
# must earn their keep from a state token, exactly as they do today.
_US_CITY_MARKERS = (
    "new york city", "nyc", "los angeles", "chicago", "houston", "phoenix",
    "philadelphia", "san antonio", "san diego", "dallas", "san jose",
    "austin", "jacksonville", "fort worth", "charlotte", "indianapolis",
    "seattle", "denver", "washington dc", "nashville", "oklahoma city",
    "las vegas", "louisville", "baltimore", "milwaukee", "albuquerque",
    "tucson", "fresno", "sacramento", "kansas city", "mesa", "atlanta",
    "omaha", "colorado springs", "raleigh", "virginia beach", "long beach",
    "oakland", "minneapolis", "tulsa", "bakersfield", "wichita", "arlington",
    "aurora", "tampa", "new orleans", "cleveland", "honolulu", "anaheim",
    "lexington", "stockton", "corpus christi", "henderson", "riverside",
    "newark", "saint paul", "st paul", "santa ana", "cincinnati", "irvine",
    "orlando", "pittsburgh", "st louis", "saint louis", "greensboro",
    "jersey city", "anchorage", "lincoln", "plano", "durham", "buffalo",
    "chandler", "chula vista", "toledo", "madison", "gilbert", "reno",
    "fort wayne", "north las vegas", "st petersburg", "saint petersburg fl",
    "laredo", "chesapeake", "scottsdale", "glendale", "irving", "winston salem",
    "fremont", "san bernardino", "boise", "richmond", "huntsville", "spokane",
    "des moines", "modesto", "fayetteville", "tacoma", "oxnard", "fontana",
    "montgomery", "moreno valley", "shreveport", "yonkers", "akron",
    "huntington beach", "little rock", "augusta", "amarillo", "mobile",
    "grand rapids", "salt lake city", "huntington", "tallahassee",
    "grand prairie", "overland park", "knoxville", "worcester",
    "brownsville", "newport news", "santa clarita", "providence",
    "garden grove", "oceanside", "rancho cucamonga", "santa rosa",
    "chattanooga", "fort lauderdale", "tempe", "sioux falls", "springfield mo",
    "peoria", "pembroke pines", "elk grove", "salem", "corona", "eugene",
    "mcallen", "lancaster", "palmdale", "salinas", "hayward", "pasadena",
    "sunnyvale", "macon", "pomona", "escondido", "clarksville", "joliet",
    "rockford", "torrance", "naperville", "bridgeport", "hollywood",
    "paterson", "savannah", "mesquite", "syracuse", "surprise", "alexandria",
    "roseville", "thornton", "murfreesboro", "carrollton", "denton",
    "midland", "elgin", "west valley city", "clearwater", "waterbury",
    "gainesville", "billings", "lowell", "san buenaventura", "pueblo",
    "high point", "west covina", "murrieta", "boulder", "greeley",
    "menlo park", "mountain view", "palo alto", "santa clara", "san mateo",
    "redwood city", "cupertino", "bellevue", "redmond", "kirkland",
    "boca raton", "san francisco", "boston", "detroit", "memphis",
    "miami", "portland", "el paso", "brooklyn", "queens", "bronx",
    "staten island", "manhattan", "silicon valley", "bay area",
    "research triangle", "san luis obispo", "ann arbor", "chapel hill",
    "college station", "state college", "iowa city", "west lafayette",
)
_US_CITY_RE = re.compile(
    r"\b(" + "|".join(re.escape(m) for m in _US_CITY_MARKERS) + r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Tiers 7/8
# ---------------------------------------------------------------------------
_REMOTE_RE = re.compile(
    r"\b(remote|anywhere|work[ -]?from[ -]?home|wfh|virtual|telecommute"
    r"|home[ -]?based|distributed)\b", re.IGNORECASE)
_MULTI_LOC_RE = re.compile(r"\b(\d+\s+locations?|multiple locations?"
                           r"|various locations?|locations?)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# How a board separates the regions of a multi-region posting.
_REGION_SPLIT_RE = re.compile(r"[,;/|]|\band\b|\bor\b|&", re.IGNORECASE)
# A segment that is nothing but the US, allowing a leading "Remote".
_US_ONLY_SEGMENT_RE = re.compile(
    r"^(?:remote\s*[-–—]?\s*)?(?:the\s+)?"
    r"(?:us|usa|u\.?s\.?a?\.?|united states(?: of america)?)"
    r"(?:\s+(?:offices?|only|based|remote))?$", re.IGNORECASE)


def _multi_region_includes_us(location: str) -> bool:
    """Does this string list the US as one of several regions?

    Requires the US to be its own SEGMENT, not merely present. The looser test
    ("does the string contain 'US'") re-opened a leak the foreign-city tier
    had already closed: the Workday row

        "IN-HR-Gurgaon-US Holidays-DLF Cyber Greens"

    is a Gurgaon office whose name happens to contain "US Holidays". Splitting
    on real region separators puts that entire string in one segment, which is
    not a US segment, while "US and Canada Offices" and "Remote, Canada;
    Remote, United States" both yield one.
    """
    return any(_US_ONLY_SEGMENT_RE.match(seg.strip())
               for seg in _REGION_SPLIT_RE.split(str(location or "")) if seg.strip())


def _fold(text: str) -> str:
    """Strip diacritics so an accented spelling matches its ASCII marker.

    Boards publish "Remote, Sao Paulo" and "Remote, São Paulo" interchangeably,
    and only the first was being caught — the accented spelling sailed past the
    foreign-city tier and a Brazilian req scored 80 in a US-only slice. Same
    for "Zürich", "Kraków", "Montréal", "Bogotá", "München". Applied to the
    location text before every marker test; the caller's original string is
    left untouched so the uppercase state-code rule still sees real casing.
    """
    return "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))


def _normalize_country(value: Any) -> str:
    """Fold an ATS country field onto a comparable token."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = text.replace(".", " ").replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


def state_position_match(location: str, *, loose: bool = False) -> bool:
    """True when a two-letter US state code sits where a state actually sits.

    ``loose`` additionally accepts the leading-prefix and uppercase-token
    forms. It is off by default because those positions are indistinguishable
    from a foreign country code, so only tier 5 — which runs after every
    foreign marker has been ruled out — may switch it on.
    """
    text = str(location or "")
    if any(rx.search(text) for rx in _STATE_POSITION_STRICT):
        return True
    if not loose:
        return False
    return bool(_STATE_POSITION_LEADING.search(text)
                or _STATE_POSITION_UPPER.search(text))


def has_foreign_marker(location: str) -> bool:
    """True when the string names a non-US country, province, ISO prefix or
    foreign hub city. Used by callers that need the signal without the full
    keep/drop decision (e.g. the auto-apply intake logs why it skipped)."""
    text = str(location or "")
    if not text.strip():
        return False
    return bool(_NON_US_COUNTRY_RE.search(text)
                or _CA_PROVINCE_RE.search(text)
                or _FOREIGN_ISO_PREFIX_RE.search(text)
                or _NON_US_CITY_RE.search(text))


def keep_us(location: Any,
            country_code: Any = None,
            *,
            is_remote: bool = False,
            keep_ambiguous: bool = False,
            defer_multi: bool = False) -> bool:
    """Should a row at ``location`` appear in a US-only slice?

    ``country_code`` is the ATS's own country field when it has one; it is
    authoritative and short-circuits every heuristic below. ``is_remote`` only
    matters for a row with NO location text at all. ``keep_ambiguous`` is the
    lenient pre-hardening contract (the guest ``GUEST_US_STRICT=0`` rollback).
    ``defer_multi`` keeps "N Locations" rollups alive at the LIST stage so
    detail enrichment can resolve them; serving paths leave it False.
    """
    # 0. Authoritative country from the ATS payload.
    hint = _normalize_country(country_code)
    if hint:
        return hint in US_COUNTRY_OK

    raw = str(location or "").strip()
    if not raw:
        # No location at all: usually a remote-anywhere row. US-ness cannot be
        # proven either way, so only an explicit remote flag keeps it.
        return bool(is_remote)
    # Diacritics folded for marker matching; casing preserved, because the
    # uppercase state-code rule in tier 5 depends on it.
    loc = _fold(raw)

    # 0b. MULTI-REGION postings that explicitly name the US. A foreign country
    #     alongside an explicit US country word means the role is open in both
    #     ("US and Canada Offices", "Remote, Canada; Remote, United States"),
    #     and the US half is what this user is applying for. Checked before the
    #     foreign-country tier, which would otherwise drop the whole row on the
    #     strength of the other region — it retired a live Cerebras
    #     application whose location was literally "US and Canada Offices".
    #     Requires the US COUNTRY WORD, not a state or a city: a mention of
    #     Austin next to Bangalore is two separate offices, not an eligibility
    #     statement.
    if _multi_region_includes_us(loc) and (_NON_US_COUNTRY_RE.search(loc)
                                           or _NON_US_CITY_RE.search(loc)):
        return True

    # 1. Explicit foreign country / province / ISO-2 prefix.
    if (_NON_US_COUNTRY_RE.search(loc) or _CA_PROVINCE_RE.search(loc)
            or _FOREIGN_ISO_PREFIX_RE.search(loc)):
        return False

    # 2. Foreign hub city. This runs BEFORE the US-word tier on purpose: the
    #    row "IN-HR-Gurgaon-US Holidays-DLF Cyber Greens" is a Gurgaon posting
    #    that the word "US" (in "US Holidays") would otherwise rescue. A US
    #    namesake survives only on a spelled-out state name ("Vancouver,
    #    Washington") or STRICT positional state evidence ("Berlin, CT") —
    #    never on a stray two-letter substring ("Hyderabad-Hi-Tec City",
    #    "CA - Toronto", "CA, Ontario").
    #    2a. ...unless the hub has no US namesake at all, in which case the
    #    rescue can only ever fire on the city's OWN subdivision code
    #    ("Chennai, TN" -> Tamil Nadu; "Jakarta, ID" -> Indonesia) and there is
    #    nothing for it to rescue.
    if _NON_US_CITY_NO_NAMESAKE_RE.search(loc):
        return False
    if _NON_US_CITY_RE.search(loc):
        return bool(_US_STATE_NAME_RE.search(loc) or state_position_match(loc))

    # 3. US country word — including a bare "US", which is safe here: tiers 1
    #    and 2 removed every string carrying a foreign marker.
    if _US_WORD_RE.search(loc):
        return True

    # 4. Spelled-out state name.
    if _US_STATE_NAME_RE.search(loc):
        return True

    # 5. State code in a real state position. The loose forms are admissible
    #    here and only here, for the same reason: nothing foreign is left.
    if state_position_match(loc, loose=True):
        return True

    # 6. Unambiguous large US city with no state token.
    if _US_CITY_RE.search(loc):
        return True

    # 7. Clear remote, no foreign marker anywhere.
    if _REMOTE_RE.search(loc):
        return True

    # 8. Multi-location rollup, list stage only.
    if defer_multi and _MULTI_LOC_RE.search(loc):
        return True

    # 9. Ambiguous.
    return keep_ambiguous


def keep_us_row(job: Dict[str, Any], *,
                keep_ambiguous: bool = False,
                defer_multi: bool = False) -> bool:
    """``keep_us`` over a feed/board row dict.

    Reads ``country_hint``/``country_code``, ``location`` and ``is_remote`` —
    the field names every board row builder in this repo already emits.
    """
    job = job or {}
    country = job.get("country_hint") or job.get("country_code") or job.get("country")
    return keep_us(job.get("location"), country,
                   is_remote=bool(job.get("is_remote")),
                   keep_ambiguous=keep_ambiguous,
                   defer_multi=defer_multi)


def keep_us_posting(doc: Dict[str, Any], *,
                    keep_ambiguous: bool = False,
                    defer_multi: bool = False) -> bool:
    """``keep_us`` over a raw ``job_postings`` inventory document.

    The inventory stores geo as ``locations: [{raw, country, key}]`` (measured:
    every active doc has 0 or 1 entries). Callers that hold the posting rather
    than a rendered row use this so the auto-apply intake and the browse feed
    cannot drift apart on what "US" means.
    """
    doc = doc or {}
    locations = doc.get("locations") or []
    first = locations[0] if isinstance(locations, list) and locations else {}
    if not isinstance(first, dict):
        first = {"raw": first}
    return keep_us(first.get("raw"), first.get("country"),
                   is_remote=(doc.get("work_mode") == "remote"),
                   keep_ambiguous=keep_ambiguous,
                   defer_multi=defer_multi)


#: Full state name -> USPS code. `_STATE_CODES` above answers "is this token a
#: state?", which is all the US/non-US keep rules need. A SEARCH also has to
#: answer "is 'Arizona' the same place as 'AZ'?", and nothing here did: the
#: tech-feed region filter expanded a request only into case variants of what
#: the user typed, so `regions:["Arizona"]` reached 361 active postings while
#: `regions:["AZ"]` reached 2,126 - the same state, six times the inventory.
STATE_NAME_TO_CODE: Dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "washington dc": "DC",
    "puerto rico": "PR",
}

_CODE_TO_STATE_NAME: Dict[str, str] = {}
for _name, _code in STATE_NAME_TO_CODE.items():
    _CODE_TO_STATE_NAME.setdefault(_code, _name)


def state_aliases(value: Any) -> List[str]:
    """Every spelling of one US state a stored `locations.region` might use.

    "AZ" and "Arizona" both return the code, the title-cased name and the
    common case variants, so a region filter finds the same postings whichever
    the user typed. A value that is not a US state comes back as its own case
    variants only - the filter still works, it just cannot expand.
    """
    text = str(value or "").strip()
    if not text:
        return []
    folded = " ".join(text.lower().split())
    code = STATE_NAME_TO_CODE.get(folded)
    if code is None and len(folded) == 2 and folded in _STATE_CODES:
        code = folded.upper()
    out = {text, text.upper(), text.title(), text.lower()}
    if code:
        name = _CODE_TO_STATE_NAME.get(code, "")
        out.update({code, code.lower()})
        if name:
            out.update({name, name.title(), name.upper()})
    return sorted(out)
