"""Company tiers and board discovery.

`discover()` builds data/boards.json: every company job board we can poll directly, learned from
(a) the ATS links in Simplify's full listing history (active and closed postings, any season),
(b) the hand-checked SEED_BOARDS below, and (c) the custom big-tech fetchers.
Tiers decide polling speed and whether a company is shown without posted pay.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from jobscraper import watchlist
from jobscraper.http import get_json
from jobscraper.sources import bigtech, lists

DATA = Path(__file__).resolve().parent.parent / "data"
BOARDS_FILE = DATA / "boards.json"
KNOWN_BOARDS_FILE = Path(__file__).with_name("known_boards.json")

FAANG = """Google Alphabet Meta Facebook Amazon AWS Apple Netflix Microsoft Nvidia Uber Airbnb Stripe Databricks
Snowflake Salesforce Adobe LinkedIn Bloomberg Palantir OpenAI Anthropic Coinbase Robinhood Pinterest Snap Lyft
DoorDash Instacart Roblox Dropbox Figma Datadog MongoDB Cloudflare Atlassian Block Square Twilio Duolingo Discord
Reddit Spotify Waymo Tesla SpaceX Oracle Intuit ServiceNow Qualcomm AMD TikTok ByteDance Shopify Pure_Storage
Two_Sigma Jane_Street Citadel Google_DeepMind DeepMind X_Development Nuro Zoox Aurora_Innovation Scale_AI Ramp
Plaid Brex Notion Rippling""".split()
QUANT = """Jane_Street Citadel Citadel_Securities Hudson_River_Trading HRT Jump_Trading Two_Sigma D._E._Shaw DE_Shaw
Optiver IMC_Trading IMC Susquehanna SIG DRW Five_Rings Tower_Research_Capital Virtu Akuna_Capital Old_Mission
Belvedere_Trading Radix_Trading XTX_Markets Millennium Point72 Cubist Bridgewater AQR Headlands_Technologies
Chicago_Trading_Company Peak6 Wolverine_Trading Geneva_Trading Squarepoint Squarepoint_Capital Voleon Arrowstreet PDT_Partners Qube
Vatic_Labs Engineers_Gate Quadrature Garda_Capital Valkyrie_Trading Clear_Street Flow_Traders Da_Vinci Graviton
Mako_Trading Eagle_Seven Hehmeyer Balyasny Schonfeld  Hudson_River Tradeweb Group_One_Trading Maven_Securities
Renaissance_Technologies WorldQuant Man_Group Marshall_Wace G-Research""".split()
UNICORN = """Patreon Anduril Cohere Perplexity Glean Vercel Retool Benchling Mercury Wealthfront Webflow Rubrik Hugging_Face
Together_AI Modal Cerebras Replit Anysphere Cursor Harvey Hebbia Sierra Character.AI Neuralink Lattice Navan Faire
Gusto Chime Affirm SoFi Toast Samsara Verkada Flexport Applied_Intuition Asana Airtable Okta Zscaler Squarespace
Twitch Nextdoor Quora Sigma_Computing Mistral xAI  Skild Physical_Intelligence Figure_AI Cognition Decagon
 Abridge Ironclad Fivetran dbt_Labs Snyk Wiz Arctic_Wolf Tanium Grammarly Canva Discord Epic_Games Valve
Riot_Games Chainalysis Kraken Gemini  Ripple Fireblocks Alchemy Instabase Weights_&_Biases Runway
ElevenLabs Suno Pika Groq SambaNova Lambda CoreWeave Crusoe Etched Rivian Lucid Nuro Zipline Shield_AI Saronic
Hadrian Vanta Deel  Ramp Brex Plaid Notion Figma Rippling Gong Outreach Highspot Attentive Klaviyo Navan
Whatnot Faire Gopuff Flock_Safety Samsara Motive Cockroach_Labs Temporal Supabase  PlanetScale 
Watershed Robinhood Wealthsimple  Betterment Carta Gusto Justworks Mercury  Modern_Treasury Marqeta""".split()
HIGHPAY = """Capital_One Goldman_Sachs Morgan_Stanley JPMorgan Two_Sigma Bloomberg Visa Mastercard American_Express
Intuit Adobe Salesforce Workday VMware Broadcom Cisco Arista Palo_Alto_Networks CrowdStrike Splunk Datadog
Expedia Zillow Wayfair HubSpot Yelp Etsy eBay PayPal Square Lyft Snap Roku Peloton Sony Nintendo Activision
Electronic_Arts EA Bloomberg_LP Fidelity Citi Bank_of_America BlackRock State_Street Wells_Fargo Chicago_Mercantile
CME_Group Nasdaq Cboe  Akuna""".split()

# Boards confirmed by probing the public APIs (company names on custom sites map onto these ATS boards).
SEED_BOARDS = [
    ("greenhouse", "wehrtyou", "Hudson River Trading"), ("greenhouse", "janestreet", "Jane Street"),
    ("greenhouse", "jumptrading", "Jump Trading"), ("greenhouse", "imc", "IMC Trading"),
    ("greenhouse", "towerresearchcapital", "Tower Research Capital"), ("greenhouse", "akunacapital", "Akuna Capital"),
    ("greenhouse", "point72", "Point72"), ("greenhouse", "aqr", "AQR"), ("greenhouse", "optiver", "Optiver"),
    ("greenhouse", "genevatrading", "Geneva Trading"), ("greenhouse", "pdtpartners", "PDT Partners"),
    ("greenhouse", "vaticlabs", "Vatic Labs"), ("greenhouse", "engineersgate", "Engineers Gate"),
    ("greenhouse", "clearstreet", "Clear Street"), ("greenhouse", "flowtraders", "Flow Traders"),
    ("greenhouse", "schonfeld", "Schonfeld"), ("greenhouse", "virtu", "Virtu"), ("greenhouse", "mako", "Mako Trading"),
    ("greenhouse", "xtxmarketstechnologies", "XTX Markets"), ("lever", "belvederetrading", "Belvedere Trading"),
    ("ashby", "voleon", "Voleon"), ("ashby", "quadrature", "Quadrature"),
    ("greenhouse", "airbnb", "Airbnb"), ("greenhouse", "stripe", "Stripe"), ("greenhouse", "databricks", "Databricks"),
    ("ashby", "snowflake", "Snowflake"), ("lever", "palantir", "Palantir"), ("ashby", "openai", "OpenAI"),
    ("greenhouse", "anthropic", "Anthropic"), ("greenhouse", "coinbase", "Coinbase"),
    ("greenhouse", "robinhood", "Robinhood"), ("greenhouse", "pinterest", "Pinterest"), ("greenhouse", "lyft", "Lyft"),
    ("greenhouse", "instacart", "Instacart"), ("greenhouse", "roblox", "Roblox"), ("greenhouse", "dropbox", "Dropbox"),
    ("greenhouse", "figma", "Figma"), ("ashby", "notion", "Notion"), ("ashby", "ramp", "Ramp"), ("ashby", "plaid", "Plaid"),
    ("greenhouse", "brex", "Brex"), ("greenhouse", "scaleai", "Scale AI"), ("greenhouse", "datadog", "Datadog"),
    ("greenhouse", "mongodb", "MongoDB"), ("greenhouse", "cloudflare", "Cloudflare"), ("greenhouse", "block", "Block"),
    ("greenhouse", "twilio", "Twilio"), ("greenhouse", "duolingo", "Duolingo"), ("greenhouse", "discord", "Discord"),
    ("greenhouse", "reddit", "Reddit"), ("lever", "spotify", "Spotify"), ("greenhouse", "waymo", "Waymo"),
    ("greenhouse", "spacex", "SpaceX"), ("greenhouse", "doordashusa", "DoorDash"), ("greenhouse", "bloomberg", "Bloomberg"),
    ("greenhouse", "drweng", "DRW"), ("greenhouse", "fiverings", "Five Rings"),
    ("ashby", "cohere", "Cohere"), ("ashby", "perplexity", "Perplexity"), ("ashby", "harvey", "Harvey"),
    ("ashby", "sierra", "Sierra"), ("ashby", "replit", "Replit"), ("ashby", "modal", "Modal"),
    ("ashby", "cerebras", "Cerebras"), ("greenhouse", "togetherai", "Together AI"), ("greenhouse", "neuralink", "Neuralink"),
    ("greenhouse", "vercel", "Vercel"), ("lever", "zoox", "Zoox"), ("greenhouse", "nuro", "Nuro"),
    ("greenhouse", "verkada", "Verkada"), ("greenhouse", "samsara", "Samsara"),
    ("greenhouse", "oldmissioncapital", "Old Mission"), ("greenhouse", "squarepointcapital", "Squarepoint"),
    ("greenhouse", "optiverprivate", "Optiver"), ("greenhouse", "bridgewater89", "Bridgewater"),
    ("greenhouse", "chime", "Chime"), ("greenhouse", "gusto", "Gusto"),
    ("ashby", "patreon", "Patreon"), ("ashby", "cursor", "Cursor"), ("ashby", "suno", "Suno"),
    ("lever", "outreach", "Outreach"), ("greenhouse", "motive", "Motive"), ("greenhouse", "betterment", "Betterment"),
    ("greenhouse", "justworks", "Justworks"),
]

PATTERNS = {
    "greenhouse": re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)"),
    "lever": re.compile(r"jobs\.(eu\.)?lever\.co/([\w.-]+)"),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([\w.%-]+)"),
    "workday": re.compile(r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)"),
    "workdaysite": re.compile(r"https?://(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([\w-]+)/([\w-]+)"),
    "smartrecruiters": re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)"),
    "oracle": re.compile(r"https?://([\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z]{2}(?:-[A-Z]{2})?/sites/([\w-]+)"),
}


def norm(name: str) -> str:
    """Company name -> comparison key ('D. E. Shaw & Co.' -> 'deshaw')."""
    n = name.lower().replace("_", " ")
    n = re.sub(r"\((.*?)\)", r" \1 ", n)
    n = re.sub(r"\b(inc|llc|ltd|corp(oration)?|co|company|group|technologies|technology|labs?|holdings|lp|plc|"
               r"the|usa|us|global|international)\b\.?", " ", n)
    return re.sub(r"[^a-z0-9]", "", n)


BUILTIN_TIERS = {"faang": FAANG, "quant": QUANT, "unicorn": UNICORN, "highpay": HIGHPAY}
TIER_PRIORITY = ("quant", "faang", "unicorn", "highpay")  # a company in several tiers gets the first one
SUFFIXES = {"securities", "webservices", "aws", "trading", "capital", "capitalmanagement", "deepmind", "research",
            "ai", "markets", "financial", "investments", "llc", "engineering", "hq", "americas", "university",
            "campus", "early", "earlycareers", "careers", "students", "tech", "cloud", "advisors", "partners"}


def tier_sets(cfg: dict, speedy_tiers: dict | None = None) -> dict[str, set[str]]:
    """tier -> normalized company names, from the built-in lists, config [tiers], the watchlist and Speedy."""
    extra = cfg.get("tiers", {})
    sets = {tier: {norm(x) for x in names + extra.get(tier, [])} for tier, names in BUILTIN_TIERS.items()}
    for tier, groups in watchlist.parse().items():
        for names in groups:
            sets[tier].update(norm(n) for n in names)
    for name, tier in (speedy_tiers or {}).items():
        sets[tier].add(norm(name))
    return sets


def _is_division_of(key: str, names: set[str]) -> bool:
    # 'Citadel Securities' -> citadel, 'Amazon Web Services' -> amazon, but not 'Bloomberg Philanthropies'
    return any(key.startswith(s) and key[len(s):] in SUFFIXES for s in names if len(s) >= 3)


def tier_of(company: str, sets: dict[str, set[str]]) -> str:
    k = norm(company)
    exact = next((t for t in TIER_PRIORITY if k in sets[t]), None)
    return exact or next((t for t in TIER_PRIORITY if _is_division_of(k, sets[t])), "other")


def _workday_board(m: re.Match) -> dict:
    tenant, wd, site = m.groups()
    return {"ats": "workday", "host": f"{tenant}.{wd}.myworkdayjobs.com", "tenant": tenant, "site": site}


def _workdaysite_board(m: re.Match) -> dict:
    wd, tenant, site = m.groups()
    return {"ats": "workday", "host": f"{wd}.myworkdaysite.com", "tenant": tenant, "site": site,
            "url_prefix": f"recruiting/{tenant}/"}


def _oracle_board(m: re.Match) -> dict:
    host, site = m.groups()
    return {"ats": "oracle", "host": host, "site": site}


def _lever_board(m: re.Match) -> dict:
    eu, slug = m.groups()
    return {"ats": "lever", "slug": slug, **({"eu": True} if eu else {})}


def _greenhouse_board(m: re.Match) -> dict | None:
    slug = m.group(1)
    return None if slug in ("embed", "v1") else {"ats": "greenhouse", "slug": slug}


BOARD_BUILDERS = {"workday": _workday_board, "workdaysite": _workdaysite_board, "oracle": _oracle_board,
                  "lever": _lever_board, "greenhouse": _greenhouse_board}


def board_from_url(url: str) -> dict | None:
    for ats, rx in PATTERNS.items():
        m = rx.search(url or "")
        if m:
            build = BOARD_BUILDERS.get(ats)
            return build(m) if build else {"ats": ats, "slug": m.group(1)}
    return None


def board_key(b: dict) -> str:
    if b["ats"] in ("workday", "oracle"):
        return f"{b['ats']}:{b['host']}/{b['site']}"
    return f"{b['ats']}:{b['slug'].lower()}"


def _listing_rows(log) -> list[dict]:
    log("[discover] downloading Simplify listing history ...")
    rows = get_json(lists.SIMPLIFY, timeout=120)
    try:
        rows += get_json(lists.VANSH, timeout=120)
    except Exception as e:  # noqa: BLE001
        log(f"[discover] vanshb03 list unavailable: {e}")
    return rows


def _speedy_tiers() -> dict:
    try:
        return lists.speedy()[1]
    except Exception:  # noqa: BLE001
        return {}


def _boards_from_listings(rows: list[dict]) -> dict[str, dict]:
    """key -> board, named after the company that posts on it most often."""
    boards: dict[str, dict] = {}
    names: dict[str, Counter] = {}
    for r in rows:
        b = board_from_url(r.get("url", ""))
        if b:
            k = board_key(b)
            boards.setdefault(k, b)
            names.setdefault(k, Counter())[r.get("company_name", "").strip()] += 1
    for k, n in names.items():
        boards[k]["company"] = max(n, key=n.get)
    return boards


def _curated_boards() -> list[dict]:
    """Seeds, boards found by probing the ATS APIs for watchlist companies, and the custom big-tech fetchers."""
    seeds = [{"ats": ats, "slug": slug, "company": company} for ats, slug, company in SEED_BOARDS]
    probed = json.loads(KNOWN_BOARDS_FILE.read_text())
    return seeds + [dict(b) for b in probed + bigtech.BOARDS]


def discover(cfg: dict, log=print) -> list[dict]:
    """Rebuild data/boards.json from Simplify history + seeds + custom fetchers."""
    rows = _listing_rows(log)
    sets = tier_sets(cfg, _speedy_tiers())
    boards = _boards_from_listings(rows)
    boards.update((board_key(b), b) for b in _curated_boards())
    out = [{**b, "key": k, "tier": tier_of(b["company"], sets)} for k, b in sorted(boards.items())]
    DATA.mkdir(exist_ok=True)
    BOARDS_FILE.write_text(json.dumps(out, indent=1))
    by_ats = dict(Counter(b["ats"] for b in out))
    by_tier = dict(Counter(b["tier"] for b in out))
    log(f"[discover] {len(out)} boards -> {BOARDS_FILE}  by ATS: {by_ats}  by tier: {by_tier}")
    return out


def load_boards() -> list[dict]:
    if not BOARDS_FILE.exists():
        return []
    out, seen = [], set()
    for b in json.loads(BOARDS_FILE.read_text()):
        k = b["key"].lower()  # Workday site names are case-insensitive
        if k not in seen:
            seen.add(k)
            out.append(b)
    return out
