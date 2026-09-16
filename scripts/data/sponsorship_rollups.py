"""Curated employer rollups for the H-1B sponsorship ingest.

USCIS publishes one row per (legal entity, fiscal year, NAICS, city). Amazon
files under at least nine LLCs; Deloitte under seven partnerships. A candidate
asking "does Amazon sponsor?" means the employer, not the LLC on the I-129 — so
the ingest has to roll subsidiary entities up. Getting that wrong in the other
direction is worse: ``AMAZON USA ENTERPRISE CORP D B A GRANITE AND MARBLE DESIGN
IMPORT EXP`` (Delray Beach, FL) starts with "amazon" and is a stone importer.

So this file is a **whitelist of exact normalized names**, never a prefix rule,
mirroring ``employer_registry.merge_decision``: a curated alias is one of the
three bases that may merge two employers; "it looks like a match" is not. The
ingest has two other bases, both mechanical (see ``ingest_sponsorship.py``):

  * ``employer_registry`` — both names resolve to ONE ``employers`` row whose
    identity is domain- or curated-based (``identity_confidence >= 0.80``);
  * ``tax_id_and_brand`` — identical Tax-ID last-4 AND identical first name
    token AND identical petitioner state. USCIS publishes only the last four
    digits of the EIN, so that field is **never probative alone** (9,879
    distinct values across 44k employers); it counts only with the other two.

Anything that merely shares a leading token with a group is NOT merged. It is
recorded in the run's ``rollup_candidates`` artifact so a human can promote it
here later, which is the same shape as ``employer_merge_reviews``.

Two rules decided the membership below, and both are about the PRODUCT:

  1. **One employer = one careers board.** A separately-branded subsidiary that
     runs its own board stays its own employer: LinkedIn is not Microsoft,
     Waymo is not Google, GitHub is not Microsoft. A candidate applies to a
     board, and the gap list this feeds is a list of boards to go find.
  2. **A corporate parent is not an employer.** Siemens Energy and Siemens
     Healthineers are separately listed companies, not Siemens AG divisions, so
     they are excluded from the Siemens group and say so in ``excluded``.

``members`` / ``aliases`` / ``key`` are all ``normalize_employer`` OUTPUT —
lower case, punctuation stripped, trailing corporate suffixes removed. The
test suite asserts that (``test_ingest_sponsorship.py``), because a member that
is not in normalized form silently never matches.

``aliases`` are extra lookup keys that do NOT appear in the USCIS file but DO
appear on job postings ("AWS", "TCS", "AMD"). They get their own alias document
so ``sponsorship_service.get_signal("AWS")`` resolves.

``excluded`` is not decoration: it is the audit trail for a refusal, and the
ingest asserts every excluded name stays out of the group.
"""
from __future__ import annotations

from typing import Any, Dict, List

ROLLUP_VERSION = "sponsorship-rollup-1.0.0"

BASIS_CURATED = "curated_alias"
BASIS_REGISTRY = "employer_registry"
BASIS_TAX_ID = "tax_id_and_brand"
BASIS_SINGLE = "single_entity"

# Each group: key (canonical lookup key), display (what the badge says),
# members (exact normalized USCIS names), aliases (extra lookup keys), excluded
# (names deliberately refused), note (the evidence for the decision).
GROUPS: List[Dict[str, Any]] = [
    {
        "key": "amazon",
        "display": "Amazon",
        "members": [
            "amazon com services", "amazon development center u s",
            "amazon web services", "amazon data services", "amazon advertising",
            "amazon payments", "amazon retail", "amazon studios",
            "amazon media venture", "amazon com",
        ],
        "aliases": ["aws", "amazon com", "amazon web services"],
        "excluded": ["amazon usa enterprise"],
        "note": "All members file from Amazon's Arlington VA HQ address and hire "
                "onto amazon.jobs. The Delray Beach FL stone importer trading as "
                "'AMAZON USA ENTERPRISE CORP D B A GRANITE AND MARBLE' is not Amazon.",
    },
    {
        "key": "google",
        "display": "Google",
        "members": ["google", "google public sector"],
        "aliases": ["alphabet", "google cloud"],
        "excluded": ["waymo", "verily life sciences", "deepmind"],
        "note": "Waymo/Verily run their own boards and are excluded by the "
                "one-employer-one-board rule; Alphabet is a holding company alias.",
    },
    {
        "key": "meta",
        "display": "Meta",
        "members": ["meta platforms"],
        "aliases": ["facebook", "meta platforms"],
        "excluded": ["meta it systems", "meta soft", "metabit", "metasys"],
        "note": "Menlo Park CA only. 'META IT SYSTEMS' (Charlotte NC) and "
                "'META SOFT' (Roswell GA) are unrelated staffing firms.",
    },
    {
        "key": "apple",
        "display": "Apple",
        "members": ["apple", "apple payments services"],
        "aliases": [],
        "excluded": ["apple american", "apple processing", "apple studio services",
                     "apple sentity independent productions"],
        "note": "APPLE AMERICAN GROUP is an Applebee's franchisee. The Austin/"
                "Culver City 'APPLE PROCESSING'/'APPLE STUDIO SERVICES' entities "
                "are not provably Apple Inc. from this file, so they are refused.",
    },
    {"key": "microsoft", "display": "Microsoft", "members": ["microsoft"],
     "aliases": [], "excluded": ["linkedin", "github", "microsoft mobile"],
     "note": "LinkedIn and GitHub file separately AND run separate boards."},
    {"key": "linkedin", "display": "LinkedIn", "members": ["linkedin"],
     "aliases": [], "excluded": [], "note": "Own board; deliberately not folded into Microsoft."},
    {"key": "github", "display": "GitHub", "members": ["github"],
     "aliases": [], "excluded": [], "note": "Own board; deliberately not folded into Microsoft."},

    # --- IT services / consulting -------------------------------------------
    {
        "key": "tata consultancy services",
        "display": "Tata Consultancy Services (TCS)",
        "members": ["tata consultancy services"],
        "aliases": ["tcs"],
        "excluded": ["tata technologies", "tata elxsi", "tata steel international americas",
                     "tata communications"],
        "note": "Tata Technologies, Tata Elxsi and Tata Steel are separate Tata "
                "group companies with their own boards — a shared parent is not "
                "a shared employer.",
    },
    {
        "key": "infosys", "display": "Infosys",
        "members": ["infosys", "infosys bpm", "infosys public services",
                    "infosys mccamish systems", "infosys nova"],
        "aliases": [], "excluded": [],
        "note": "All file from Infosys' Richardson TX US address.",
    },
    {
        "key": "wipro", "display": "Wipro",
        "members": ["wipro", "wipro connected services", "wipro next gen enterprise",
                    "wipro pari", "wipro telecom consulting"],
        "aliases": [], "excluded": [],
        "note": "Wipro Limited plus its US operating subsidiaries.",
    },
    {
        "key": "hcltech", "display": "HCLTech",
        "members": ["hcl america", "hcl america solutions", "hcl technologies"],
        "aliases": ["hcl", "hcl technologies"],
        "excluded": ["hcl global systems"],
        "note": "HCL AMERICA files from Santa Clara CA. 'HCL GLOBAL SYSTEMS' "
                "(Farmington Hills MI, different EIN) is an unrelated staffing "
                "firm that happens to share three letters.",
    },
    {
        "key": "cognizant", "display": "Cognizant",
        "members": ["cognizant technology solutions", "cognizant trizetto software",
                    "cognizant worldwide limited cognizant"],
        "aliases": [], "excluded": [],
        "note": "All file from College Station TX.",
    },
    {
        "key": "capgemini", "display": "Capgemini",
        "members": ["capgemini america", "capgemini government solutions"],
        "aliases": [], "excluded": [],
        "note": "Capgemini America (Chicago IL) + the federal arm.",
    },
    {
        "key": "accenture", "display": "Accenture",
        "members": ["accenture", "accenture infrastructure and capital projects",
                    "accenture federal services"],
        "aliases": [], "excluded": [],
        "note": "Accenture LLP plus wholly-owned US arms.",
    },
    {
        "key": "tech mahindra", "display": "Tech Mahindra",
        "members": ["tech mahindra americas", "tech mahindra technologies"],
        "aliases": [], "excluded": [], "note": "Both file from Bedminster NJ.",
    },
    {"key": "ltimindtree", "display": "LTIMindtree", "members": ["ltimindtree"],
     "aliases": [], "excluded": ["l and t technology services"],
     "note": "LTTS is a separately listed L&T company, not LTIMindtree."},
    {"key": "mphasis", "display": "Mphasis", "members": ["mphasis"], "aliases": [], "excluded": []},
    {"key": "ibm", "display": "IBM", "members": ["ibm"], "aliases": [],
     "excluded": ["ibm global systems", "kyndryl", "red hat"],
     "note": "Kyndryl and Red Hat run their own boards. 'IBM GLOBAL SYSTEMS' "
             "(Irving TX) is unrelated."},

    # --- Big Four / consulting ----------------------------------------------
    {
        "key": "deloitte", "display": "Deloitte",
        "members": ["deloitte consulting", "deloitte and touche", "deloitte tax",
                    "deloitte services", "deloitte touche tohmatsu services",
                    "deloitte transactions and business analytics",
                    "deloitte financial advisory services"],
        "aliases": [], "excluded": [],
        "note": "Seven US partnerships, all filing from Deloitte's Philadelphia/NYC "
                "shared-services addresses, one careers site.",
    },
    {
        "key": "pwc", "display": "PwC",
        "members": ["pwc advisory services", "pwc us tax", "pwc us consulting",
                    "pwc us business advisory", "pwc", "pwc product sales",
                    "pricewaterhousecoopers"],
        "aliases": ["pricewaterhousecoopers"], "excluded": [],
        "note": "All PwC US LLPs file from Tampa FL.",
    },
    {
        "key": "ey", "display": "EY (Ernst & Young)",
        "members": ["ernst and young u s", "ernst and young llp ey guam",
                    "ernst and young infrastructure advisors", "ey government services",
                    "ernst and young"],
        "aliases": ["ernst and young"], "excluded": ["ey legal"],
        "note": "US member firm entities filing from Secaucus NJ. 'EY LEGAL GROUP "
                "LTD' (Pomona CA) is unrelated.",
    },
    {"key": "kpmg", "display": "KPMG", "members": ["kpmg"], "aliases": [], "excluded": []},
    {"key": "mckinsey", "display": "McKinsey & Company",
     "members": ["mckinsey and company inc united states", "mckinsey and"],
     "aliases": ["mckinsey and"], "excluded": [],
     "note": "A posting saying 'McKinsey & Company' normalizes to 'mckinsey and', "
             "which is why that spelling is carried as a lookup key."},
    {"key": "boston consulting", "display": "Boston Consulting Group",
     "members": ["boston consulting"], "aliases": ["bcg"], "excluded": []},
    {"key": "bain and", "display": "Bain & Company",
     "members": ["bain and"], "aliases": [], "excluded": ["bain capital"],
     "note": "'Bain & Company' normalizes to 'bain and'. Bain Capital is a "
             "separate firm and is refused."},

    # --- Finance -------------------------------------------------------------
    {"key": "jpmorgan chase", "display": "JPMorgan Chase",
     "members": ["jpmorgan chase and", "jpmorgan chase bank n a", "j p morgan securities"],
     "aliases": ["jpmorgan", "jp morgan", "chase"], "excluded": [],
     "note": "See CLAUDE.md: JPMorgan's Oracle instance stamps postings with three "
             "different employer slugs; the sponsorship key must be one employer."},
    {"key": "goldman sachs", "display": "Goldman Sachs",
     "members": ["goldman sachs and", "goldman sachs services", "goldman sachs bank",
                 "goldman sachs wealth services l p"],
     "aliases": [], "excluded": [], "note": "All NYC."},
    {"key": "morgan stanley", "display": "Morgan Stanley",
     "members": ["morgan stanley services", "morgan stanley and",
                 "morgan stanley smith barney", "morgan stanley fund services",
                 "morgan stanley investment management", "morgan stanley private bank",
                 "morgan stanley bank n a"],
     "aliases": [], "excluded": [], "note": "All NYC."},
    {"key": "citi", "display": "Citi",
     "members": ["citibank n a", "citigroup global markets", "citigroup technology",
                 "citigroup energy", "citi ventures", "citigroup"],
     "aliases": ["citigroup", "citibank"],
     "excluded": ["citi diamonds", "citi development and construction",
                  "citi fund services ohio", "citiustech"],
     "note": "CITI FUND SERVICES OHIO was sold to Apex in 2019; CITI DIAMONDS "
             "(Atlanta) and CITI DEVELOPMENT (Guam) are unrelated, and CitiusTech "
             "is an unrelated healthcare-IT firm."},
    {"key": "bank of america", "display": "Bank of America",
     "members": ["bank of america n a", "bofa securities", "merrill lynch"],
     "aliases": ["bofa"], "excluded": []},
    {"key": "wells fargo", "display": "Wells Fargo",
     "members": ["wells fargo bank n a", "wells fargo securities",
                 "wells fargo clearing services"],
     "aliases": [], "excluded": []},
    {"key": "capital one", "display": "Capital One",
     "members": ["capital one services", "capital one national association",
                 "capital one bank usa n a", "capital one financial"],
     "aliases": [], "excluded": [],
     "note": "Both Richmond VA. employer_registry.IDENTITY_OVERRIDES already "
             "treats Capital One as one employer."},
    {"key": "american express", "display": "American Express",
     "members": ["american express travel related services", "american express",
                 "american express national bank"],
     "aliases": ["amex"], "excluded": ["gbt us llc d b a american express global business travel"],
     "note": "Amex GBT was spun out in 2022 and runs its own board."},
    {"key": "visa", "display": "Visa",
     "members": ["visa technology and operations", "visa u s a"],
     "aliases": [], "excluded": [], "note": "Both Foster City CA."},
    {"key": "mastercard", "display": "Mastercard",
     "members": ["mastercard technologies", "mastercard"],
     "aliases": [], "excluded": []},
    {"key": "paypal", "display": "PayPal",
     "members": ["paypal", "paypal data services"], "aliases": [], "excluded": []},
    {"key": "fidelity investments", "display": "Fidelity Investments",
     "members": ["fidelity technology group llc d b a fidelity investments",
                 "fidelity brokerage services llc d b a fidelity investments",
                 "fidelity investments", "fmr"],
     "aliases": ["fidelity"], "excluded": ["fidelity national information services",
                                           "fidelity national financial"],
     "note": "FIS and FNF are different companies that share the word 'Fidelity'."},
    {"key": "charles schwab", "display": "Charles Schwab",
     "members": ["charles schwab and", "charles schwab"], "aliases": ["schwab"], "excluded": []},
    {"key": "blackrock", "display": "BlackRock",
     "members": ["blackrock financial management", "blackrock"], "aliases": [],
     "excluded": ["blackstone"], "note": "BlackRock and Blackstone are different firms."},
    {"key": "stripe", "display": "Stripe", "members": ["stripe"], "aliases": [], "excluded": []},
    {"key": "block", "display": "Block (Square)", "members": ["block"],
     "aliases": ["square"],
     "excluded": ["block chain solutions", "block institute", "block otoole and murphy",
                  "block scientific"],
     "note": "BLOCK INC (Oakland CA) only — the four refusals are a Georgia "
             "consultancy, a Brooklyn nonprofit, a law firm and a lab supplier."},

    # --- Semis / hardware ----------------------------------------------------
    {"key": "nvidia", "display": "NVIDIA", "members": ["nvidia"], "aliases": [], "excluded": []},
    {"key": "intel", "display": "Intel",
     "members": ["intel", "intel federal", "intel americas"],
     "aliases": [], "excluded": ["intel grids"],
     "note": "Chandler AZ. 'INTEL GRIDS INC' (Pleasanton CA) is unrelated."},
    {"key": "qualcomm", "display": "Qualcomm",
     "members": ["qualcomm technologies", "qualcomm atheros",
                 "qualcomm innovation center", "qualcomm"],
     "aliases": [], "excluded": [], "note": "All San Diego CA."},
    {"key": "amd", "display": "AMD", "members": ["advanced micro devices"],
     "aliases": ["advanced micro devices"], "excluded": []},
    {"key": "micron", "display": "Micron",
     "members": ["micron technology", "micron idaho semiconductor manufacturing triton",
                 "micron semiconductor products"],
     "aliases": [], "excluded": [], "note": "All Boise ID."},
    {"key": "texas instruments", "display": "Texas Instruments",
     "members": ["texas instruments"], "aliases": ["ti"], "excluded": []},
    {"key": "broadcom", "display": "Broadcom", "members": ["broadcom"],
     "aliases": [], "excluded": ["vmware"],
     "note": "VMware is Broadcom-owned but still recruits under its own brand."},
    {"key": "cisco", "display": "Cisco", "members": ["cisco systems", "cisco"],
     "aliases": [], "excluded": []},
    {"key": "applied materials", "display": "Applied Materials",
     "members": ["applied materials"], "aliases": [], "excluded": []},
    {"key": "lam research", "display": "Lam Research", "members": ["lam research"],
     "aliases": [], "excluded": []},
    {"key": "kla", "display": "KLA", "members": ["kla"], "aliases": [],
     "excluded": ["kla elementary"], "note": "KLA ELEMENTARY LLC is a Miami school."},
    {"key": "asml", "display": "ASML", "members": ["asml"], "aliases": [], "excluded": []},
    {"key": "tsmc", "display": "TSMC",
     "members": ["tsmc arizona", "tsmc technology", "tsmc north america", "tsmc"],
     "aliases": [], "excluded": []},
    {"key": "globalfoundries", "display": "GlobalFoundries",
     "members": ["globalfoundries u s", "globalfoundries us 2", "globalfoundries u s 2",
                 "globalfoundries"],
     "aliases": [], "excluded": [], "note": "All Malta NY."},
    {"key": "samsung electronics", "display": "Samsung Electronics",
     "members": ["samsung electronics america", "samsung semiconductor",
                 "samsung research america", "samsung austin semiconductor",
                 "samsung electronics home appliances america"],
     "aliases": ["samsung"], "excluded": ["samsung sdi america", "samsung e and a"],
     "note": "Samsung SDI is a separate Samsung-group affiliate, not Samsung Electronics."},
    {"key": "renesas", "display": "Renesas",
     "members": ["renesas electronics america", "renesas design north america",
                 "renesas design technology"],
     "aliases": [], "excluded": []},
    {"key": "infineon", "display": "Infineon",
     "members": ["infineon technologies americas", "infineon technologies"],
     "aliases": [], "excluded": []},
    {"key": "analog devices", "display": "Analog Devices", "members": ["analog devices"],
     "aliases": ["adi"], "excluded": []},
    {"key": "marvell", "display": "Marvell", "members": ["marvell semiconductor"],
     "aliases": [], "excluded": []},
    {"key": "western digital", "display": "Western Digital",
     "members": ["western digital technologies"], "aliases": [], "excluded": ["sandisk"],
     "note": "SanDisk was spun back out as a separate public company in 2025."},
    {"key": "dell", "display": "Dell Technologies",
     "members": ["dell usa l p", "dell products l p", "dell marketing l p",
                 "dell financial services l l c", "emc"],
     "aliases": ["dell technologies", "dell emc"], "excluded": ["dell painting"],
     "note": "All Round Rock TX including EMC Corporation. 'DELL PAINTING CORP' "
             "(North Arlington NJ) is a painting contractor."},
    {"key": "hewlett packard enterprise", "display": "Hewlett Packard Enterprise",
     "members": ["hewlett packard enterprise"], "aliases": ["hpe"], "excluded": ["hp"],
     "note": "HP Inc and HPE have been separate companies since 2015."},
    {"key": "hp", "display": "HP Inc.", "members": ["hp"], "aliases": [], "excluded": []},

    # --- Software / internet -------------------------------------------------
    {"key": "oracle", "display": "Oracle",
     "members": ["oracle america", "oracle financial services software", "oracle"],
     "aliases": [], "excluded": ["oracle robotics"],
     "note": "'ORACLE ROBOTICS CORP' (Chicago IL) is unrelated."},
    {"key": "salesforce", "display": "Salesforce", "members": ["salesforce"],
     "aliases": [], "excluded": ["slack technologies", "tableau software"],
     "note": "Slack and Tableau still recruit under their own brands."},
    {"key": "sap", "display": "SAP",
     "members": ["sap america", "sap labs", "sap industries", "sap global marketing"],
     "aliases": [], "excluded": [], "note": "All Newtown Square PA."},
    {"key": "adobe", "display": "Adobe", "members": ["adobe"], "aliases": [], "excluded": []},
    {"key": "servicenow", "display": "ServiceNow", "members": ["servicenow"],
     "aliases": [], "excluded": []},
    {"key": "intuit", "display": "Intuit", "members": ["intuit"],
     "aliases": [], "excluded": [],
     "note": "Credit Karma merges in via the employers registry (shared verified "
             "domain), not from this table."},
    {"key": "workday", "display": "Workday", "members": ["workday"], "aliases": [], "excluded": []},
    {"key": "snowflake", "display": "Snowflake", "members": ["snowflake"], "aliases": [], "excluded": []},
    {"key": "databricks", "display": "Databricks", "members": ["databricks"], "aliases": [], "excluded": []},
    {"key": "palantir", "display": "Palantir", "members": ["palantir technologies"],
     "aliases": [], "excluded": []},
    {"key": "openai", "display": "OpenAI", "members": ["openai opco", "openai"],
     "aliases": [], "excluded": []},
    {"key": "anthropic", "display": "Anthropic", "members": ["anthropic pbc", "anthropic"],
     "aliases": [], "excluded": []},
    {"key": "netflix", "display": "Netflix", "members": ["netflix"],
     "aliases": [], "excluded": []},
    {"key": "uber", "display": "Uber", "members": ["uber technologies"],
     "aliases": [], "excluded": ["uber freight"],
     "note": "Uber Freight raised outside capital and runs its own board."},
    {"key": "airbnb", "display": "Airbnb", "members": ["airbnb", "airbnb payments"],
     "aliases": [], "excluded": []},
    {"key": "tiktok", "display": "TikTok",
     "members": ["tiktok", "tiktok usds joint venture", "tiktok u s data security"],
     "aliases": [], "excluded": ["bytedance"],
     "note": "ByteDance and TikTok post to separate boards and are kept separate; "
             "the two USDS entities share TikTok's EIN last-4 and Culver City address."},
    {"key": "bytedance", "display": "ByteDance", "members": ["bytedance"],
     "aliases": [], "excluded": []},
    {"key": "walmart", "display": "Walmart",
     "members": ["wal mart associates", "walmart", "walmart global tech"],
     "aliases": ["walmart labs", "walmart global tech"], "excluded": [],
     "note": "Walmart files as WAL-MART ASSOCIATES INC; no posting will ever say that."},
    {"key": "tesla", "display": "Tesla", "members": ["tesla"], "aliases": [], "excluded": []},
    {"key": "rivian", "display": "Rivian", "members": ["rivian automotive", "rivian"],
     "aliases": [], "excluded": ["rivian and volkswagen group technologies",
                                 "rivian and vw group technology"],
     "note": "The Rivian/VW joint venture is a separate employer with its own hiring."},
    {"key": "lucid motors", "display": "Lucid Motors", "members": ["lucid"],
     "aliases": [], "excluded": ["lucid software", "lucid technologies",
                                 "lucid behavioral health"],
     "note": "LUCID GROUP USA INC (Newark CA). Lucid Software (Lucidchart, South "
             "Jordan UT) is a different company that also hires engineers."},
    {"key": "collins aerospace", "display": "Collins Aerospace (RTX)",
     "members": ["rockwell collins inc d b a collins aerospace",
                 "b e aerospace inc d b a collins aerospace",
                 "arinc incorporated d b a collins aerospace",
                 "rohr inc dba collins aerospace",
                 "hamilton sundstrand corporation d b a collins aerospace",
                 "goodrich corporation d b a collins aerospace"],
     "aliases": [], "excluded": ["rtx", "raytheon"],
     "note": "Six legal entities all trading as Collins Aerospace (the DBA is in "
             "the filed name, which is the employer's own statement). RTX the "
             "parent files separately and is kept separate."},
    {"key": "geico", "display": "GEICO",
     "members": ["government employee insurance company geico", "geico"],
     "aliases": [], "excluded": []},
    {"key": "ust", "display": "UST", "members": ["ust"], "aliases": [], "excluded": []},
    {"key": "mathworks", "display": "MathWorks", "members": ["mathworks"],
     "aliases": [], "excluded": []},
    {"key": "siemens", "display": "Siemens",
     "members": ["siemens", "siemens industry", "siemens industry software",
                 "siemens mobility", "siemens financial services"],
     "aliases": ["siemens digital industries software", "siemens eda"],
     "excluded": ["siemens energy", "siemens gamesa renewable energy",
                  "siemens medical solutions", "siemens healthcare diagnostics",
                  "siemens healthineers"],
     "note": "Siemens Energy (2020) and Siemens Healthineers are separately listed "
             "public companies with their own boards — a shared brand is not a "
             "shared employer.",
     },
    {"key": "general motors", "display": "General Motors",
     "members": ["general motors", "general motors financial"], "aliases": ["gm"], "excluded": []},
    {"key": "ford", "display": "Ford", "members": ["ford motor", "ford motor credit"],
     "aliases": [], "excluded": []},
    {"key": "optum", "display": "Optum (UnitedHealth)",
     "members": ["optum services", "optum care", "optum select management",
                 "optum services puerto rico", "optum"],
     "aliases": [], "excluded": [], "note": "All Minnetonka MN."},
    {"key": "cvs health", "display": "CVS Health",
     "members": ["cvs shared services resources", "cvs pharmacy", "cvs rx services",
                 "cvs orlando fl distribution", "caremark", "aetna resources", "cvs health"],
     "aliases": ["cvs", "aetna"], "excluded": [],
     "note": "All Woonsocket RI — CVS, Caremark and Aetna file from one address."},
    {"key": "electronic arts", "display": "Electronic Arts",
     "members": ["electronic arts", "electronic arts tiburon a florida"],
     "aliases": ["ea"], "excluded": []},
    {"key": "verizon", "display": "Verizon",
     "members": ["verizon data services", "verizon corp resources",
                 "verizon business network services", "verizon corporate resources",
                 "verizon services", "verizon value", "verizon"],
     "aliases": [], "excluded": [], "note": "All Basking Ridge NJ."},
    {"key": "at and t", "display": "AT&T",
     "members": ["at and t services", "at and t"], "aliases": ["att"], "excluded": []},
]


def validate_groups(groups: List[Dict[str, Any]] = None) -> List[str]:
    """Return a list of structural problems with the table (empty == healthy).

    Checked here rather than in a test alone because the ingest runs it at
    startup: a member claimed by two parents is a silent double-count, and a
    member that is not in normalized form can never match anything.
    """
    from services.sponsorship_service import normalize_employer

    groups = GROUPS if groups is None else groups
    problems: List[str] = []
    owner: Dict[str, str] = {}
    keys = set()
    for g in groups:
        key = g.get("key") or ""
        if key in keys:
            problems.append(f"duplicate group key {key!r}")
        keys.add(key)
        if normalize_employer(key) != key:
            problems.append(f"group key {key!r} is not normalized "
                            f"(normalize_employer -> {normalize_employer(key)!r})")
        names = list(g.get("members") or []) + list(g.get("aliases") or [])
        excluded = set(g.get("excluded") or [])
        for name in names + list(excluded):
            if normalize_employer(name) != name:
                problems.append(f"{key}: {name!r} is not normalized "
                                f"(-> {normalize_employer(name)!r})")
        for name in names:
            if name in excluded:
                problems.append(f"{key}: {name!r} is both a member and excluded")
            prev = owner.get(name)
            if prev and prev != key:
                problems.append(f"{name!r} claimed by both {prev!r} and {key!r}")
            owner[name] = key
    return problems


def member_index(groups: List[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """normalized USCIS name -> the group that claims it."""
    groups = GROUPS if groups is None else groups
    out: Dict[str, Dict[str, Any]] = {}
    for g in groups:
        for name in (g.get("members") or []):
            out[name] = g
    return out


def excluded_index(groups: List[Dict[str, Any]] = None) -> Dict[str, str]:
    """normalized name -> the group key that explicitly REFUSED it."""
    groups = GROUPS if groups is None else groups
    out: Dict[str, str] = {}
    for g in groups:
        for name in (g.get("excluded") or []):
            out[name] = g["key"]
    return out
