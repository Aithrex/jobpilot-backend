"""
H-1B sponsorship tiers for the career-pages catalog (non-Workday companies).

AUTO-GENERATED from FY2024/FY2025 research (USCIS H-1B Employer Data Hub
via h1bgrader/myvisajobs/h1bdata). Keyed by career_pages_catalog slug.
Tier ~ recent annual approvals: "high" 500+, "medium" 50-499, "low" 1-49.
Absent slugs are UNKNOWN (no filing evidence), not proven non-sponsors.
"""
from __future__ import annotations

from typing import Dict, Tuple

# slug -> (tier, approx annual approvals)
CAREER_H1B_SPONSORS: Dict[str, Tuple[str, int]] = {
    "abnormalsecurity": ("medium", 60),  # Abnormal Security
    "abridge": ("low", 15),  # Abridge
    "adyen": ("low", 30),  # Adyen
    "affirm": ("medium", 130),  # Affirm
    "agilityrobotics": ("low", 20),  # Agility Robotics
    "airbnb": ("medium", 179),  # Airbnb
    "aircall": ("low", 5),  # Aircall
    "airtable": ("medium", 60),  # Airtable
    "akunacapital": ("medium", 80),  # Akuna Capital
    "alchemy": ("low", 20),  # Alchemy
    "alloy": ("low", 20),  # Alloy
    "alltrails": ("low", 5),  # AllTrails
    "ambiencehealthcare": ("low", 8),  # Ambience Healthcare
    "amplitude": ("medium", 55),  # Amplitude
    "amwins": ("low", 5),  # Amwins
    "andurilindustries": ("low", 12),  # Anduril Industries
    "anthropic": ("medium", 100),  # Anthropic
    "anyscale": ("low", 30),  # Anyscale
    "apolloio": ("low", 20),  # Apollo.io
    "arcadia": ("low", 15),  # Arcadia
    "asana": ("medium", 90),  # Asana
    "atbay": ("low", 10),  # At-Bay
    "aurorainnovation": ("medium", 120),  # Aurora Innovation
    "axonius": ("low", 15),  # Axonius
    "baseten": ("low", 10),  # Baseten
    "benchling": ("medium", 60),  # Benchling
    "betterment": ("low", 25),  # Betterment
    "binance": ("low", 10),  # Binance
    "bitgo": ("low", 25),  # BitGo
    "blockchain": ("low", 15),  # Blockchain.com
    "boxinc": ("medium", 100),  # Box
    "braze": ("medium", 55),  # Braze
    "brex": ("medium", 75),  # Brex
    "brightmachines": ("low", 15),  # Bright Machines
    "brilliant": ("low", 5),  # Brilliant
    "bungie": ("low", 30),  # Bungie
    "calendly": ("low", 15),  # Calendly
    "calm": ("low", 10),  # Calm
    "carbonhealth": ("low", 20),  # Carbon Health
    "carta": ("medium", 75),  # Carta
    "cfsenergy": ("low", 40),  # Commonwealth Fusion Systems
    "character": ("low", 20),  # Character.AI
    "charliehealth": ("low", 10),  # Charlie Health
    "checkr": ("medium", 50),  # Checkr
    "chime": ("medium", 90),  # Chime
    "circleci": ("low", 25),  # CircleCI
    "clari": ("low", 40),  # Clari
    "clickhouse": ("low", 25),  # ClickHouse
    "clipboard": ("low", 15),  # Clipboard Health
    "cloudflare": ("medium", 120),  # Cloudflare
    "cloverhealth": ("low", 25),  # Clover Health
    "coalition": ("low", 25),  # Coalition
    "cockroachlabs": ("low", 30),  # Cockroach Labs
    "cognition": ("low", 30),  # Cognition
    "cohere": ("low", 15),  # Cohere
    "coinbase": ("medium", 147),  # Coinbase
    "collibra": ("low", 30),  # Collibra
    "column": ("low", 10),  # Column
    "commonroom": ("low", 5),  # Common Room
    "consensys": ("low", 15),  # Consensys
    "coursera": ("medium", 60),  # Coursera
    "cribl": ("low", 20),  # Cribl
    "crossriverbank": ("low", 30),  # Cross River
    "crusoe": ("low", 40),  # Crusoe
    "cultureamp": ("low", 5),  # Culture Amp
    "current": ("low", 15),  # Current
    "cursor": ("low", 20),  # Cursor (Anysphere)
    "dagsterlabs": ("low", 5),  # Dagster Labs
    "dashlane": ("low", 10),  # Dashlane
    "databricks": ("medium", 339),  # Databricks
    "datadog": ("medium", 78),  # Datadog
    "dbtlabsinc": ("low", 15),  # dbt Labs
    "decagon": ("low", 5),  # Decagon
    "deel": ("low", 40),  # Deel
    "dialpad": ("low", 30),  # Dialpad
    "discord": ("medium", 60),  # Discord
    "docker": ("low", 40),  # Docker
    "doordashusa": ("medium", 462),  # DoorDash
    "doximity": ("low", 30),  # Doximity
    "dronedeploy": ("low", 10),  # DroneDeploy
    "dropbox": ("medium", 110),  # Dropbox
    "duolingo": ("medium", 60),  # Duolingo
    "dynotherapeutics": ("low", 5),  # Dyno Therapeutics
    "elastic": ("medium", 80),  # Elastic
    "elevenlabs": ("low", 15),  # ElevenLabs
    "enigmaio": ("low", 10),  # Enigma
    "entrata": ("medium", 50),  # Entrata
    "epicgames": ("medium", 80),  # Epic Games
    "etched": ("low", 10),  # Etched
    "ethoslife": ("low", 20),  # Ethos Life
    "expel": ("low", 10),  # Expel
    "faire": ("medium", 60),  # Faire
    "faradayfuture": ("low", 20),  # Faraday Future
    "fictiv": ("low", 10),  # Fictiv
    "figma": ("medium", 60),  # Figma
    "figure": ("low", 40),  # Figure
    "figureai": ("low", 40),  # Figure
    "fireblocks": ("low", 20),  # Fireblocks
    "fireworksai": ("low", 25),  # Fireworks AI
    "fivetran": ("medium", 55),  # Fivetran
    "flatironhealth": ("medium", 80),  # Flatiron Health
    "flexport": ("medium", 75),  # Flexport
    "flyzipline": ("low", 40),  # Zipline
    "formationbio": ("low", 15),  # Formation Bio
    "formlabs": ("low", 30),  # Formlabs
    "found": ("low", 5),  # Found
    "galaxydigitalservices": ("low", 20),  # Galaxy Digital
    "gemini": ("low", 40),  # Gemini
    "generatebiomedicines": ("low", 20),  # Generate Biomedicines
    "gleanwork": ("medium", 80),  # Glean
    "goatgroup": ("low", 20),  # GOAT Group
    "gohighlevel": ("low", 10),  # HighLevel
    "gongio": ("low", 30),  # Gong
    "gopuff": ("medium", 60),  # Gopuff
    "grafanalabs": ("low", 15),  # Grafana Labs
    "greenhouse": ("low", 25),  # Greenhouse Software
    "guild": ("low", 15),  # Guild
    "gusto": ("medium", 75),  # Gusto
    "harvey": ("low", 20),  # Harvey
    "hex": ("low", 10),  # Hex
    "highspot": ("medium", 60),  # Highspot
    "hightouch": ("low", 15),  # Hightouch
    "hippo70": ("low", 20),  # Hippo Insurance
    "honeycomb": ("low", 5),  # Honeycomb
    "hubspot": ("medium", 110),  # HubSpot
    "hugeinc": ("low", 10),  # Huge
    "humaninterest": ("low", 20),  # Human Interest
    "ideo": ("low", 5),  # IDEO
    "imc": ("medium", 140),  # IMC Trading
    "includedhealth": ("low", 40),  # Included Health
    "instabase": ("medium", 50),  # Instabase
    "instacart": ("medium", 150),  # Instacart
    "instawork": ("low", 20),  # Instawork
    "intercom": ("low", 40),  # Intercom
    "iovancebiotherapeutics": ("low", 20),  # Iovance Biotherapeutics
    "iterable": ("low", 25),  # Iterable
    "jumptrading": ("medium", 80),  # Jump Trading
    "justworks": ("low", 40),  # Justworks
    "kalshi": ("low", 15),  # Kalshi
    "kayak": ("low", 30),  # KAYAK
    "keystonestrategy": ("low", 25),  # Keystone Strategy
    "khanacademy": ("low", 10),  # Khan Academy
    "klaviyo": ("medium", 60),  # Klaviyo
    "kodiak": ("low", 20),  # Kodiak Robotics
    "komodohealth": ("low", 30),  # Komodo Health
    "kraken": ("low", 30),  # Kraken
    "labelbox": ("low", 10),  # Labelbox
    "langchain": ("low", 5),  # LangChain
    "lattice": ("low", 20),  # Lattice
    "launchdarkly": ("low", 20),  # LaunchDarkly
    "legendcareers": ("medium", 60),  # Legend Biotech
    "lithic": ("low", 10),  # Lithic
    "lyft": ("medium", 200),  # Lyft
    "lyrahealth": ("low", 40),  # Lyra Health
    "mark43": ("low", 10),  # Mark43
    "markforged": ("low", 10),  # Markforged
    "marqeta": ("low", 40),  # Marqeta
    "masterclass": ("low", 10),  # MasterClass
    "matchgroup": ("medium", 80),  # Match Group
    "matillion": ("low", 5),  # Matillion
    "mavenclinic": ("low", 20),  # Maven Clinic
    "maymobility": ("low", 25),  # May Mobility
    "medium": ("low", 5),  # Medium
    "melio": ("low", 20),  # Melio
    "mercari": ("low", 30),  # Mercari
    "mercor": ("low", 10),  # Mercor
    "mercury": ("medium", 50),  # Mercury
    "merge": ("low", 10),  # Merge
    "middesk": ("low", 5),  # Middesk
    "mistral": ("low", 5),  # Mistral AI
    "mixpanel": ("low", 25),  # Mixpanel
    "modal": ("low", 6),  # Modal
    "modernhealth": ("low", 10),  # Modern Health
    "moderntreasury": ("low", 15),  # Modern Treasury
    "mongodb": ("medium", 160),  # MongoDB
    "moonpay": ("low", 10),  # MoonPay
    "motherduck": ("low", 5),  # MotherDuck
    "motional": ("medium", 60),  # Motional
    "mythicalgames": ("low", 5),  # Mythical Games
    "natera": ("medium", 100),  # Natera
    "neteasegames": ("low", 30),  # NetEase Games
    "netlify": ("low", 5),  # Netlify
    "netskope": ("medium", 100),  # Netskope
    "newsbreak": ("low", 20),  # NewsBreak
    "nextdoor": ("low", 40),  # Nextdoor
    "nextinsurance66": ("low", 30),  # Next Insurance
    "nium": ("low", 15),  # Nium
    "notable": ("low", 10),  # Notable
    "notion": ("medium", 80),  # Notion
    "nuro": ("medium", 100),  # Nuro
    "okta": ("medium", 140),  # Okta
    "omadahealth": ("low", 15),  # Omada Health
    "onestudyteam": ("low", 5),  # OneStudyTeam
    "openai": ("medium", 125),  # OpenAI
    "openevidence": ("low", 5),  # OpenEvidence
    "openspace": ("low", 10),  # OpenSpace
    "openx": ("low", 20),  # OpenX
    "optiver": ("medium", 120),  # Optiver
    "orcasecurity": ("low", 5),  # Orca Security
    "oscar": ("medium", 80),  # Oscar Health
    "outreach": ("medium", 50),  # Outreach
    "outschool": ("low", 5),  # Outschool
    "pagerduty": ("low", 40),  # PagerDuty
    "palantir": ("medium", 200),  # Palantir Technologies
    "parafin": ("low", 10),  # Parafin
    "pathai": ("low", 25),  # PathAI
    "peloton": ("medium", 50),  # Peloton
    "perplexity": ("medium", 70),  # Perplexity
    "phantom": ("low", 10),  # Phantom
    "physicalintelligence": ("low", 8),  # Physical Intelligence
    "pilothq": ("low", 15),  # Pilot
    "pinterest": ("medium", 200),  # Pinterest
    "plaid": ("medium", 100),  # Plaid
    "planetlabs": ("low", 30),  # Planet
    "polymarket": ("low", 5),  # Polymarket
    "poshmark": ("low", 40),  # Poshmark
    "postman": ("medium", 50),  # Postman
    "project44": ("low", 30),  # project44
    "qualified": ("low", 10),  # Qualified
    "quince": ("low", 20),  # Quince
    "ramp": ("medium", 100),  # Ramp
    "recursionpharmaceuticals": ("low", 30),  # Recursion
    "reddit": ("medium", 130),  # Reddit
    "render": ("low", 5),  # Render
    "replit": ("low", 20),  # Replit
    "rho": ("low", 10),  # Rho
    "ripple": ("medium", 60),  # Ripple
    "ro": ("low", 25),  # Ro
    "robinhood": ("medium", 140),  # Robinhood
    "roblox": ("medium", 180),  # Roblox
    "rocketlab": ("low", 10),  # Rocket Lab
    "rockstargames": ("medium", 60),  # Rockstar Games
    "root": ("low", 30),  # Root Insurance
    "rti": ("low", 15),  # Real-Time Innovations
    "salesloft": ("low", 15),  # Salesloft
    "sambanovasystems": ("medium", 60),  # SambaNova Systems
    "samsara": ("medium", 100),  # Samsara
    "sardine": ("low", 10),  # Sardine
    "scaleai": ("low", 47),  # Scale AI
    "scopely": ("low", 40),  # Scopely
    "sharkninjaoperatingllc": ("medium", 60),  # SharkNinja
    "shieldai": ("low", 25),  # Shield AI
    "sierra": ("low", 15),  # Sierra
    "sigmacomputing": ("low", 30),  # Sigma Computing
    "singlestore": ("low", 30),  # SingleStore
    "snorkelai": ("low", 15),  # Snorkel AI
    "soundcloud71": ("low", 10),  # SoundCloud
    "speak": ("low", 10),  # Speak
    "spire": ("low", 10),  # Spire Global
    "spotify": ("medium", 90),  # Spotify
    "sproutsocial": ("low", 20),  # Sprout Social
    "squarespace": ("medium", 50),  # Squarespace
    "stashinvest": ("low", 10),  # Stash
    "stitchfix": ("low", 30),  # Stitch Fix
    "stockx": ("low", 20),  # StockX
    "stripe": ("medium", 211),  # Stripe
    "substack": ("low", 5),  # Substack
    "suno": ("low", 5),  # Suno
    "synthesia": ("low", 5),  # Synthesia
    "tala": ("low", 15),  # Tala
    "tanium": ("medium", 60),  # Tanium
    "taxbit": ("low", 10),  # TaxBit
    "teleport": ("low", 10),  # Teleport
    "temporaltechnologies": ("low", 15),  # Temporal Technologies
    "tennr": ("low", 5),  # Tennr
    "thenewyorktimes": ("medium", 50),  # The New York Times
    "thirtymadison": ("low", 10),  # Thirty Madison
    "togetherai": ("low", 30),  # Together AI
    "torq": ("low", 5),  # Torq
    "traba": ("low", 5),  # Traba
    "tripadvisor": ("medium", 60),  # Tripadvisor
    "truveta": ("low", 15),  # Truveta
    "tulip": ("low", 10),  # Tulip
    "twilio": ("medium", 130),  # Twilio
    "twitch": ("medium", 90),  # Twitch
    "udemy": ("low", 40),  # Udemy
    "unit": ("low", 10),  # Unit
    "upgrade": ("low", 30),  # Upgrade
    "urbancompass": ("medium", 60),  # Compass
    "vanta": ("medium", 50),  # Vanta
    "varomoney": ("low", 25),  # Varo Bank
    "veeva": ("medium", 65),  # Veeva Systems
    "vercel": ("low", 40),  # Vercel
    "verkada": ("medium", 100),  # Verkada
    "voleon": ("low", 35),  # The Voleon Group
    "vts": ("low", 20),  # VTS
    "waabi": ("low", 5),  # Waabi
    "watershed": ("low", 20),  # Watershed
    "waymo": ("medium", 158),  # Waymo
    "webflow": ("low", 25),  # Webflow
    "wehrtyou": ("medium", 120),  # Hudson River Trading
    "wikimedia": ("low", 15),  # Wikimedia Foundation
    "wizinc": ("medium", 100),  # Wiz
    "workos": ("low", 5),  # WorkOS
    "workstream": ("low", 15),  # Workstream
    "writer": ("low", 20),  # Writer
    "xai": ("low", 47),  # xAI
    "xometry": ("low", 15),  # Xometry
    "yext": ("low", 40),  # Yext
    "youcom": ("low", 10),  # You.com
    "zip": ("low", 40),  # Zip
    "zocdoc": ("medium", 50),  # Zocdoc
    "zoox": ("medium", 274),  # Zoox
    "zyngacareers": ("medium", 100),  # Zynga
}
