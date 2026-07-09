"""
H-1B sponsorship tiers for Workday tenant catalog companies.

AUTO-GENERATED from FY2024/FY2025 research (USCIS H-1B Employer Data Hub,
myvisajobs, h1bdata.info — see scripts/ docs). Keyed by tenant slug.
Tier ~ recent annual approvals: "high" 500+, "medium" 50-499, "low" 1-49.
Absent tenants are UNKNOWN (no evidence), not proven non-sponsors.
"""
from __future__ import annotations

from typing import Dict, Tuple

# tenant -> (tier, approx annual approvals)
H1B_SPONSORS: Dict[str, Tuple[str, int]] = {
    "3m": ("medium", 200),  # 3M
    "7eleven": ("medium", 50),  # 7-Eleven
    "abbott": ("high", 500),  # Abbott
    "accenture": ("high", 4000),  # Accenture
    "acg": ("low", 20),  # AAA - The Auto Club Group
    "adient": ("low", 30),  # Adient
    "adobe": ("high", 1000),  # Adobe
    "adventhealth": ("medium", 100),  # AdventHealth
    "adventisthealthcare": ("low", 20),  # Adventist HealthCare
    "aep": ("low", 30),  # American Electric Power
    "ag": ("low", 30),  # Airbus
    "agilent": ("medium", 150),  # Agilent Technologies
    "aig": ("medium", 300),  # AIG
    "ais": ("low", 10),  # Applied Information Sciences
    "albanymed": ("medium", 50),  # Albany Medical Center
    "alcoa": ("low", 20),  # Alcoa
    "alcon": ("medium", 100),  # Alcon
    "alegeus": ("low", 10),  # Alegeus
    "alight": ("medium", 80),  # Alight Solutions
    "alkami": ("low", 30),  # Alkami Technology
    "alliance": ("medium", 100),  # Nissan
    "alliancedata": ("low", 40),  # Bread Financial
    "allstate": ("medium", 200),  # Allstate
    "alston": ("low", 10),  # Alston & Bird
    "amadeus": ("medium", 100),  # Amadeus
    "amat": ("high", 800),  # Applied Materials
    "ameren": ("low", 30),  # Ameren
    "amfam": ("medium", 50),  # American Family Insurance
    "amgen": ("high", 500),  # Amgen
    "analogdevices": ("medium", 300),  # Analog Devices
    "archildrens": ("low", 15),  # Arkansas Children's
    "aresmgmt": ("low", 30),  # Ares Management
    "areteir": ("low", 10),  # Arete
    "armaninollp": ("low", 20),  # Armanino LLP
    "asml": ("medium", 300),  # ASML
    "associatedbank": ("low", 5),  # Associated Bank
    "assurant": ("low", 30),  # Assurant
    "astound": ("low", 10),  # Astound Broadband
    "astrazeneca": ("high", 500),  # AstraZeneca
    "atcllc": ("medium", 50),  # American Tower
    "athenahealth": ("medium", 100),  # athenahealth
    "att": ("high", 700),  # AT&T
    "autodesk": ("medium", 300),  # Autodesk
    "autonation": ("low", 10),  # AutoNation
    "avera": ("low", 30),  # Avera Health
    "avnet": ("low", 30),  # Avnet
    "axalta": ("low", 30),  # Axalta Coating Systems
    "axiscapital": ("low", 20),  # AXIS Capital
    "azenta": ("low", 30),  # Azenta Life Sciences
    "bacardi": ("low", 15),  # Bacardi
    "bah": ("medium", 100),  # Booz Allen Hamilton
    "baincapital": ("medium", 50),  # Bain Capital
    "bakerhughes": ("medium", 300),  # Baker Hughes
    "bakertilly": ("medium", 60),  # Baker Tilly
    "bannerhealth": ("medium", 100),  # Banner Health
    "barclays": ("medium", 440),  # Barclays
    "barrywehmiller": ("low", 15),  # Barry-Wehmiller
    "baxter": ("medium", 200),  # Baxter International
    "bbinsurance": ("low", 10),  # Brown & Brown Insurance
    "bdo": ("medium", 150),  # BDO
    "bdx": ("medium", 150),  # BD
    "beautyhealth": ("low", 5),  # The Beauty Health Company
    "becu": ("low", 10),  # BECU
    "bhs": ("medium", 50),  # Baptist Health
    "bigcommerce": ("low", 20),  # BigCommerce
    "biibhr": ("medium", 150),  # Biogen
    "biotechne": ("low", 30),  # Bio-Techne
    "blackstone": ("medium", 100),  # Blackstone
    "bmc": ("medium", 100),  # Boston Medical Center
    "bmo": ("medium", 200),  # BMO
    "boeing": ("medium", 400),  # Boeing
    "borgwarner": ("medium", 80),  # BorgWarner
    "bristolmyerssquibb": ("high", 500),  # Bristol Myers Squibb
    "broadcom": ("high", 700),  # Broadcom
    "broadridge": ("medium", 150),  # Broadridge
    "brookfield": ("medium", 60),  # Brookfield
    "brownhealth": ("medium", 60),  # Brown University Health
    "browserstack": ("low", 20),  # BrowserStack
    "burlington": ("low", 20),  # Burlington Stores
    "caci": ("low", 40),  # CACI
    "cadence": ("high", 500),  # Cadence Design Systems
    "calyx": ("low", 30),  # Calyx (Perceptive)
    "cambridgeassociates": ("low", 20),  # Cambridge Associates
    "capgroup": ("medium", 150),  # Capital Group
    "capitalone": ("high", 1500),  # Capital One
    "cardinalhealth": ("medium", 200),  # Cardinal Health
    "carmax": ("low", 30),  # CarMax
    "carrier": ("medium", 100),  # Carrier
    "cartech": ("low", 10),  # Carpenter Technology
    "cat": ("medium", 300),  # Caterpillar
    "cbh": ("low", 30),  # Cherry Bekaert
    "cc": ("low", 15),  # Chanel
    "ccf": ("medium", 300),  # Cleveland Clinic
    "cdk": ("medium", 80),  # CDK Global
    "cdw": ("medium", 50),  # CDW
    "chemours": ("low", 20),  # Chemours
    "chevron": ("medium", 300),  # Chevron
    "chewy": ("medium", 100),  # Chewy
    "chipotle": ("low", 20),  # Chipotle
    "choa": ("low", 30),  # Children's Healthcare of Atlanta
    "cibc": ("low", 30),  # CIBC
    "ciena": ("medium", 100),  # Ciena
    "cigna": ("high", 500),  # The Cigna Group
    "cisco": ("high", 1500),  # Cisco
    "citi": ("high", 1000),  # Citi
    "clearwateranalytics": ("medium", 50),  # Clearwater Analytics
    "clorox": ("low", 30),  # Clorox
    "cloudera": ("medium", 100),  # Cloudera
    "cmegroup": ("medium", 100),  # CME Group
    "cna": ("low", 30),  # CNA Insurance
    "columbiasportswearcompany": ("low", 15),  # Columbia Sportswear
    "comcast": ("high", 600),  # Comcast
    "commercebank": ("low", 15),  # Commerce Bank
    "conagrabrands": ("low", 30),  # Conagra Brands
    "conocophillips": ("medium", 100),  # ConocoPhillips
    "cookchildrens": ("low", 20),  # Cook Children's
    "corpay": ("low", 20),  # Corpay
    "cox": ("medium", 150),  # Cox Communications
    "cranecompany": ("low", 10),  # Crane Company
    "crinetics": ("low", 20),  # Crinetics Pharmaceuticals
    "crowdstrike": ("medium", 200),  # CrowdStrike
    "crowe": ("medium", 100),  # Crowe
    "csgi": ("low", 30),  # CSG International
    "cswg": ("low", 20),  # C&S Wholesale Grocers
    "cvshealth": ("high", 600),  # CVS Health
    "davita": ("low", 30),  # DaVita
    "db": ("medium", 450),  # Deutsche Bank
    "dell": ("high", 800),  # Dell Technologies
    "denverhealth": ("low", 30),  # Denver Health
    "diageo": ("low", 20),  # Diageo
    "dimensional": ("medium", 60),  # Dimensional Fund Advisors
    "dinebrands": ("low", 5),  # Dine Brands
    "disney": ("medium", 300),  # Disney
    "dlapiper": ("low", 20),  # DLA Piper
    "dow": ("medium", 150),  # Dow
    "dtna": ("medium", 100),  # Daimler Truck North America
    "dukeenergy": ("medium", 80),  # Duke Energy
    "dupont": ("medium", 100),  # DuPont
    "dxctechnology": ("medium", 400),  # DXC Technology
    "easyservice": ("medium", 50),  # Bon Secours Mercy Health
    "ebay": ("high", 500),  # eBay
    "edwards": ("medium", 150),  # Edwards Lifesciences
    "eisenhower": ("low", 10),  # Eisenhower Health
    "electrolux": ("low", 20),  # Electrolux
    "enbridge": ("low", 30),  # Enbridge
    "entegris": ("medium", 80),  # Entegris
    "epicorsoftware": ("medium", 50),  # Epicor Software
    "equifax": ("medium", 200),  # Equifax
    "equinix": ("medium", 150),  # Equinix
    "ercot": ("low", 15),  # ERCOT
    "erm": ("low", 20),  # ERM
    "evercommerce": ("low", 10),  # EverCommerce
    "everestre": ("low", 30),  # Everest Re Group
    "eversource": ("low", 30),  # Eversource Energy
    "evolent": ("medium", 50),  # Evolent Health
    "expedia": ("high", 700),  # Expedia Group
    "ffive": ("medium", 100),  # F5
    "fifththird": ("medium", 100),  # Fifth Third Bank
    "finastra": ("medium", 50),  # Finastra
    "firstnational": ("low", 10),  # FNBO
    "fis": ("high", 500),  # FIS
    "fiserv": ("medium", 400),  # Fiserv
    "flextronics": ("medium", 150),  # Flex
    "fmc": ("low", 40),  # FMC Corporation
    "fmr": ("high", 800),  # Fidelity Investments
    "fractal": ("medium", 200),  # Fractal Analytics
    "frostbank": ("low", 10),  # Frost Bank
    "gaig": ("low", 20),  # Great American Insurance Group
    "gapinc": ("low", 40),  # Gap Inc.
    "gartner": ("medium", 150),  # Gartner
    "gdit": ("low", 40),  # GDIT
    "geaerospace": ("medium", 200),  # GE Aerospace
    "geico": ("medium", 150),  # GEICO
    "geisinger": ("medium", 150),  # Geisinger
    "gen": ("medium", 50),  # Gen Digital
    "generalmotors": ("high", 800),  # General Motors
    "genpact": ("medium", 200),  # Genpact
    "ghr": ("high", 1500),  # Bank of America
    "gilead": ("high", 500),  # Gilead Sciences
    "globalfoundries": ("medium", 300),  # GlobalFoundries
    "globalhr": ("medium", 200),  # RTX
    "globusmedical": ("low", 40),  # Globus Medical
    "gmh": ("low", 20),  # Grady Memorial Hospital
    "gn": ("low", 10),  # GN Group
    "goodrx": ("low", 20),  # GoodRx
    "goodwinprocter": ("low", 15),  # Goodwin Procter
    "goodyear": ("medium", 80),  # Goodyear Tire & Rubber
    "granite": ("low", 5),  # Granite Telecommunications
    "greendotcorp": ("low", 20),  # Green Dot
    "gsk": ("medium", 300),  # GSK
    "guardianlife": ("medium", 150),  # Guardian Life
    "guidehouse": ("medium", 100),  # Guidehouse
    "guidewire": ("medium", 200),  # Guidewire
    "gunder": ("low", 5),  # Gunderson Dettmer
    "gundersenhealth": ("low", 20),  # Gundersen Health System
    "haier": ("medium", 50),  # GE Appliances (Haier)
    "hancockwhitney": ("low", 5),  # Hancock Whitney
    "hcsc": ("medium", 200),  # Health Care Service Corporation
    "hedgeserv": ("low", 30),  # HedgeServ
    "heinz": ("medium", 100),  # Kraft Heinz
    "hhc": ("medium", 50),  # Hartford HealthCare
    "highmarkhealth": ("medium", 100),  # Highmark Health
    "honorhealth": ("low", 20),  # HonorHealth
    "hp": ("medium", 400),  # HP
    "hpe": ("high", 500),  # Hewlett Packard Enterprise
    "hshs": ("low", 30),  # Hospital Sisters Health System
    "humana": ("medium", 300),  # Humana
    "huntington": ("medium", 80),  # Huntington National Bank
    "hysteryale": ("low", 15),  # Hyster-Yale
    "hyvee": ("low", 10),  # Hy-Vee
    "iberdrola": ("low", 30),  # Iberdrola
    "illumina": ("medium", 200),  # Illumina
    "imh": ("medium", 80),  # Intermountain Health
    "ineos": ("low", 10),  # INEOS
    "ingrammicro": ("medium", 50),  # Ingram Micro
    "insbrk": ("low", 10),  # Risk Strategies
    "intapp": ("low", 30),  # Intapp
    "intel": ("high", 2000),  # Intel
    "invenergyllc": ("low", 30),  # Invenergy
    "invesco": ("medium", 150),  # Invesco
    "itron": ("low", 20),  # Itron
    "itw": ("medium", 60),  # Illinois Tool Works
    "jabil": ("medium", 100),  # Jabil
    "jackson": ("low", 30),  # Jackson National Life
    "jda": ("medium", 200),  # Blue Yonder
    "jeffersonhealth": ("medium", 150),  # Jefferson Health
    "jll": ("medium", 100),  # JLL
    "justfab": ("low", 10),  # TechStyle (JustFab/Fabletics)
    "kansashealthsystem": ("medium", 50),  # University of Kansas Health System
    "kaweahhealth": ("low", 10),  # Kaweah Health
    "kbr": ("medium", 80),  # KBR
    "kemper": ("low", 30),  # Kemper
    "keybank": ("medium", 100),  # KeyBank
    "kla": ("medium", 400),  # KLA
    "kone": ("low", 20),  # KONE
    "kslaw": ("low", 10),  # King & Spalding
    "kumc": ("medium", 150),  # University of Kansas Medical Center
    "kyndryl": ("medium", 300),  # Kyndryl
    "labcorp": ("medium", 150),  # Labcorp
    "leidos": ("medium", 80),  # Leidos
    "levistraussandco": ("low", 30),  # Levi Strauss & Co.
    "libertyglobal": ("low", 20),  # Liberty Global
    "livenation": ("low", 20),  # Live Nation
    "logitech": ("low", 40),  # Logitech
    "lplfinancial": ("medium", 100),  # LPL Financial
    "lseg": ("medium", 150),  # LSEG
    "luriechildrens": ("low", 30),  # Lurie Children's Hospital
    "lvhn": ("medium", 50),  # Lehigh Valley Health Network
    "macu": ("low", 5),  # Mountain America Credit Union
    "magna": ("medium", 100),  # Magna International
    "mallinckrodt": ("low", 20),  # Mallinckrodt Pharmaceuticals
    "mantech": ("low", 15),  # ManTech
    "markelcorp": ("low", 20),  # Markel
    "marvell": ("medium", 400),  # Marvell Technology
    "massgeneralbrigham": ("medium", 300),  # Mass General Brigham
    "massmutual": ("medium", 200),  # MassMutual
    "mastercard": ("high", 600),  # Mastercard
    "mckesson": ("medium", 200),  # McKesson
    "mdlz": ("medium", 100),  # Mondelez
    "medline": ("medium", 100),  # Medline Industries
    "medtronic": ("high", 500),  # Medtronic
    "meijer": ("low", 20),  # Meijer
    "memorialhermann": ("medium", 60),  # Memorial Hermann
    "methodisthealthsystem": ("low", 20),  # Methodist Health System
    "michelinhr": ("medium", 80),  # Michelin
    "micron": ("high", 1000),  # Micron Technology
    "mitre": ("low", 40),  # MITRE
    "mksinst": ("medium", 60),  # MKS Instruments
    "mmc": ("medium", 200),  # Marsh McLennan
    "modernatx": ("medium", 200),  # Moderna
    "motorolasolutions": ("medium", 200),  # Motorola Solutions
    "mpc": ("low", 40),  # Marathon Petroleum
    "ms": ("high", 1000),  # Morgan Stanley
    "msigna": ("low", 10),  # MSIG North America
    "msmc": ("low", 20),  # Mount Sinai Medical Center (Miami)
    "mtb": ("medium", 100),  # M&T Bank
    "mtmus": ("low", 5),  # Mazda Toyota Manufacturing
    "mufgub": ("medium", 150),  # MUFG
    "multicare": ("low", 30),  # MultiCare Health System
    "musc": ("medium", 150),  # Medical University of South Carolina
    "mvphealthcare": ("low", 15),  # MVP Health Care
    "mwe": ("low", 10),  # McDermott Will & Emery
    "nasdaq": ("medium", 100),  # Nasdaq
    "nationwide": ("medium", 200),  # Nationwide
    "nationwidechildrens": ("medium", 80),  # Nationwide Children's Hospital
    "nb": ("low", 30),  # Neuberger Berman
    "ncino": ("low", 30),  # nCino
    "ncnu": ("low", 10),  # AAA Northern California, Nevada & Utah
    "nexperia": ("low", 20),  # Nexperia
    "ngc": ("low", 40),  # Northrop Grumman
    "nike": ("medium", 340),  # Nike
    "nordstrom": ("medium", 50),  # Nordstrom
    "northwesternmutual": ("medium", 100),  # Northwestern Mutual
    "novartis": ("high", 500),  # Novartis
    "nshs": ("medium", 200),  # Northwell Health
    "ntrs": ("medium", 150),  # Northern Trust
    "nttlimited": ("medium", 100),  # NTT
    "nvidia": ("high", 1500),  # NVIDIA
    "nxp": ("medium", 300),  # NXP Semiconductors
    "nytimes": ("low", 30),  # The New York Times
    "nyuhs": ("low", 40),  # NYU Langone Health
    "ochsner": ("medium", 100),  # Ochsner Health
    "oneok": ("low", 15),  # ONEOK
    "optiv": ("low", 20),  # Optiv
    "oumedicine": ("medium", 50),  # OU Health
    "ouryahoo": ("medium", 200),  # Yahoo
    "oxy": ("medium", 80),  # Occidental Petroleum
    "pacificlife": ("low", 40),  # Pacific Life
    "palig": ("low", 10),  # Pan-American Life Insurance Group
    "paypal": ("high", 800),  # PayPal
    "pennmutual": ("low", 15),  # Penn Mutual
    "peopleplus": ("medium", 50),  # Standard Chartered
    "pfizer": ("high", 600),  # Pfizer
    "pg": ("medium", 300),  # Procter & Gamble
    "philips": ("medium", 300),  # Philips
    "pipersandler": ("low", 20),  # Piper Sandler
    "plains": ("low", 15),  # Plains All American
    "pluralsight": ("low", 20),  # Pluralsight
    "pnc": ("medium", 300),  # PNC
    "portlandgeneral": ("low", 10),  # Portland General Electric
    "prismahealth": ("medium", 60),  # Prisma Health
    "pru": ("medium", 300),  # Prudential Financial
    "ptc": ("medium", 100),  # PTC
    "puma": ("low", 10),  # PUMA
    "pwc": ("high", 1500),  # PwC
    "q2ebanking": ("low", 40),  # Q2
    "qtsdatacenters": ("low", 15),  # QTS Data Centers
    "qualcomm": ("high", 1500),  # Qualcomm
    "qualys": ("medium", 100),  # Qualys
    "quantiphi": ("medium", 200),  # Quantiphi
    "racetrac": ("low", 10),  # RaceTrac
    "rackspace": ("medium", 100),  # Rackspace Technology
    "rakuten": ("medium", 80),  # Rakuten Americas
    "raymondjames": ("medium", 80),  # Raymond James
    "rb": ("low", 15),  # Federal Reserve System
    "redhat": ("medium", 300),  # Red Hat
    "regeneron": ("medium", 300),  # Regeneron
    "regions": ("low", 40),  # Regions Bank
    "relx": ("medium", 100),  # RELX
    "remitly": ("medium", 80),  # Remitly
    "resolutionlife": ("low", 10),  # Resolution Life
    "revvity": ("low", 40),  # Revvity
    "rialtocapital": ("low", 5),  # Rialto Capital
    "riministreet": ("low", 30),  # Rimini Street
    "roberthalf": ("medium", 80),  # Robert Half
    "rochester": ("medium", 350),  # University of Rochester
    "rocket": ("medium", 100),  # Rocket Companies
    "rockwellautomation": ("medium", 150),  # Rockwell Automation
    "roswellpark": ("medium", 50),  # Roswell Park Comprehensive Cancer Center
    "rsm": ("medium", 200),  # RSM
    "russell": ("low", 20),  # Russell Investments
    "ryansg": ("low", 15),  # Ryan Specialty
    "ryder": ("low", 30),  # Ryder
    "sailpoint": ("medium", 50),  # SailPoint
    "saintlukes": ("low", 30),  # Saint Luke's Health System
    "saks": ("low", 20),  # Saks Fifth Avenue
    "salesforce": ("high", 1500),  # Salesforce
    "sanford": ("medium", 80),  # Sanford Health
    "sanofi": ("medium", 400),  # Sanofi
    "santander": ("medium", 100),  # Santander
    "sbdinc": ("medium", 80),  # Stanley Black & Decker
    "sds": ("medium", 100),  # Samsung SDS
    "sec": ("medium", 200),  # Samsung Electronics America
    "sedgwick": ("low", 30),  # Sedgwick
    "shell": ("medium", 300),  # Shell
    "shi": ("low", 30),  # SHI International
    "simcorp": ("low", 10),  # SimCorp
    "skadden": ("low", 20),  # Skadden
    "skechers": ("low", 15),  # Skechers
    "sonyglobal": ("medium", 200),  # Sony
    "spgi": ("medium", 300),  # S&P Global
    "sphera": ("low", 20),  # Sphera
    "ssctech": ("medium", 350),  # SS&C Technologies
    "ssmh": ("medium", 80),  # SSM Health
    "starrcompanies": ("low", 20),  # Starr Companies
    "statestreet": ("medium", 260),  # State Street
    "stblaw": ("low", 15),  # Simpson Thacher & Bartlett
    "stout": ("low", 15),  # Stout
    "stryker": ("medium", 300),  # Stryker
    "sumitomopharma": ("low", 30),  # Sumitomo Pharma America
    "sutterhealth": ("medium", 80),  # Sutter Health
    "svb": ("medium", 100),  # First Citizens Bank (SVB)
    "synchronyfinancial": ("medium", 150),  # Synchrony Financial
    "synechron": ("medium", 250),  # Synechron
    "synnex": ("medium", 80),  # TD SYNNEX
    "takeda": ("medium", 400),  # Takeda
    "talentmanagementsolution": ("medium", 60),  # Illinois Tool Works (ERCS)
    "target": ("medium", 220),  # Target
    "taskus": ("low", 10),  # TaskUs
    "tcenergy": ("low", 30),  # TC Energy
    "td": ("medium", 200),  # TD Bank
    "tealium": ("low", 10),  # Tealium
    "tel": ("medium", 100),  # Tokyo Electron
    "teleperformance": ("low", 10),  # Teleperformance
    "telstra": ("low", 5),  # Telstra
    "telusinternational": ("low", 20),  # TELUS International
    "temenos": ("low", 30),  # Temenos
    "terex": ("low", 15),  # Terex
    "texascapitalbank": ("low", 15),  # Texas Capital Bank
    "thales": ("low", 30),  # Thales
    "theapexgroup": ("low", 20),  # Apex Group
    "thehartford": ("medium", 150),  # The Hartford
    "thomsonreuters": ("medium", 200),  # Thomson Reuters
    "tiaa": ("medium", 200),  # TIAA
    "tihinsurance": ("low", 10),  # TIH Insurance (CRC Group)
    "tjx": ("medium", 100),  # TJX Companies
    "tmnas": ("low", 20),  # Philadelphia Insurance (Tokio Marine NA)
    "tmobile": ("high", 500),  # T-Mobile
    "tnsi": ("low", 15),  # Transaction Network Services
    "transunion": ("medium", 200),  # TransUnion
    "travelers": ("medium", 300),  # Travelers
    "trimble": ("medium", 150),  # Trimble
    "trinityhealth": ("medium", 100),  # Trinity Health
    "troweprice": ("medium", 100),  # T. Rowe Price
    "truist": ("medium", 300),  # Truist
    "tsys": ("medium", 150),  # Global Payments (TSYS)
    "ttc": ("low", 20),  # The Toro Company
    "tuftsmedicine": ("medium", 80),  # Tufts Medicine
    "uasys": ("medium", 100),  # UAMS
    "umb": ("low", 15),  # UMB Financial
    "ummc": ("medium", 100),  # University of Mississippi Medical Center
    "unisys": ("medium", 100),  # Unisys
    "unum": ("low", 40),  # Unum
    "uobgroup": ("low", 5),  # UOB
    "uoflhealth": ("medium", 50),  # UofL Health
    "usbank": ("medium", 300),  # U.S. Bank
    "uvmhealth": ("medium", 50),  # University of Vermont Health Network
    "valleyhealth": ("low", 20),  # Valley Health System
    "vanguard": ("medium", 400),  # Vanguard
    "vcuhealth": ("medium", 100),  # VCU Health
    "velera": ("low", 15),  # Velera
    "verizon": ("high", 700),  # Verizon
    "vfc": ("low", 30),  # VF Corporation
    "viatris": ("low", 40),  # Viatris
    "visa": ("high", 800),  # Visa
    "vumc": ("medium", 250),  # Vanderbilt University Medical Center
    "warnerbros": ("medium", 200),  # Warner Bros. Discovery
    "wawa": ("low", 10),  # Wawa
    "websteronline": ("low", 10),  # Webster Bank (HSA Bank)
    "wellington": ("medium", 80),  # Wellington Management
    "westernalliancebank": ("low", 20),  # Western Alliance Bank
    "westernunion": ("medium", 100),  # Western Union
    "wexinc": ("low", 30),  # WEX
    "wf": ("high", 800),  # Wells Fargo
    "whataburger": ("low", 5),  # Whataburger
    "williams": ("low", 30),  # Williams-Sonoma
    "wintrust": ("low", 10),  # Wintrust Financial
    "wje": ("low", 15),  # Wiss, Janney, Elstner Associates
    "workday": ("high", 600),  # Workday
    "workiva": ("low", 30),  # Workiva
    "worldpay": ("medium", 100),  # Worldpay
    "wsfsbank": ("low", 5),  # WSFS Bank
    "wvumedicine": ("medium", 80),  # WVU Medicine
    "xcelenergy": ("low", 30),  # Xcel Energy
    "xenergy": ("low", 20),  # X-energy
    "zayo": ("low", 30),  # Zayo
    "zealandpharma": ("low", 10),  # Zealand Pharma
    "zebra": ("medium", 150),  # Zebra Technologies
    "zeissgroup": ("medium", 80),  # Carl Zeiss Group
    "zoetis": ("medium", 80),  # Zoetis
    "zoom": ("medium", 300),  # Zoom
}
