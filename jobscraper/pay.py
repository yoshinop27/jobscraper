"""Reputation-based intern pay estimates ($/hr, SWE-type roles), used when a posting doesn't list pay.

These are rough midpoints from publicly reported intern pay (e.g. levels.fyi, company pay-transparency ranges)
as of 2025-26. They're estimates, not quotes: the dashboard marks them "est.", and posted pay always wins.
Override or extend them in config.toml under [pay_estimates] ("Company Name" = 55).
Annualized quant intern pay is often quoted as ~$200-250k, i.e. roughly $100-120/hr.
"""
from __future__ import annotations

from jobscraper.companies import norm

PAY_EST: dict[str, float] = {
    # quant / trading
    "Jane Street": 115, "Citadel": 120, "Citadel Securities": 120, "Hudson River Trading": 120, "Jump Trading": 110,
    "Two Sigma": 110, "D. E. Shaw": 115, "DE Shaw": 115, "Optiver": 100, "IMC Trading": 100, "IMC": 100,
    "Susquehanna": 100, "SIG": 100, "DRW": 100, "Five Rings": 120, "Tower Research Capital": 100,
    "Akuna Capital": 70, "Old Mission": 85, "Belvedere Trading": 60, "PEAK6": 55, "Chicago Trading Company": 90,
    "Virtu": 70, "AQR": 70, "Point72": 85, "Cubist": 85, "Millennium": 80, "Bridgewater": 60, "XTX Markets": 110,
    "Radix Trading": 100, "Headlands Technologies": 90, "Squarepoint": 90, "Voleon": 90, "Arrowstreet": 70,
    "PDT Partners": 100, "Vatic Labs": 90, "Engineers Gate": 90, "Wolverine Trading": 60, "Geneva Trading": 55,
    "Flow Traders": 60, "Clear Street": 55, "Schonfeld": 70, "Balyasny": 75, "Marshall Wace": 70, "G-Research": 90,
    "Man Group": 60, "Quadrature": 90, "Garda Capital": 70, "Mako Trading": 60, "Maven Securities": 70,
    "Renaissance Technologies": 120, "WorldQuant": 50,
    # FAANG+ / big tech
    "Google": 55, "Alphabet": 55, "Google DeepMind": 60, "Meta": 57, "Amazon": 53, "Apple": 52, "Netflix": 60,
    "Microsoft": 52, "NVIDIA": 55, "Uber": 55, "Airbnb": 60, "Stripe": 60, "Databricks": 60, "Snowflake": 60,
    "Salesforce": 52, "Adobe": 50, "LinkedIn": 55, "Bloomberg": 50, "Palantir": 58, "OpenAI": 75, "Anthropic": 75,
    "Coinbase": 60, "Robinhood": 60, "Pinterest": 55, "Snap": 55, "Lyft": 55, "DoorDash": 55, "Instacart": 55,
    "Roblox": 65, "Dropbox": 55, "Figma": 60, "Datadog": 55, "MongoDB": 50, "Cloudflare": 45, "Atlassian": 50,
    "Block": 55, "Square": 55, "Twilio": 50, "Duolingo": 50, "Discord": 55, "Reddit": 55, "Spotify": 50, "Waymo": 55,
    "Tesla": 40, "SpaceX": 40, "Oracle": 45, "Intuit": 50, "ServiceNow": 50, "Qualcomm": 45, "AMD": 45,
    "TikTok": 50, "ByteDance": 50, "Shopify": 45, "Ramp": 60, "Plaid": 55, "Brex": 55, "Notion": 55,
    "Scale AI": 55, "Rippling": 55, "Pure Storage": 50, "Zoox": 50, "Nuro": 50, "Aurora Innovation": 50,
    "X Development": 55, "IBM": 35, "Cisco": 45, "Arista Networks": 50, "Palo Alto Networks": 50,
    "CrowdStrike": 45, "Workday": 50, "VMware": 45, "Broadcom": 45, "Expedia Group": 45, "Zillow": 45,
    "Wayfair": 45, "HubSpot": 45, "Yelp": 45, "Etsy": 45, "eBay": 45, "PayPal": 45, "Roku": 50, "Twitch": 55,
    # unicorns / AI labs
    "Anduril": 50, "Cohere": 50, "Perplexity": 60, "Glean": 55, "Vercel": 50, "Retool": 55, "Benchling": 50,
    "Mercury": 55, "Wealthfront": 50, "Webflow": 50, "Rubrik": 55, "Together AI": 55, "Modal": 60,
    "Cerebras": 50, "Replit": 55, "Anysphere": 70, "Cursor": 70, "Harvey": 60, "Hebbia": 60, "Sierra": 60,
    "Neuralink": 45, "Applied Intuition": 55, "Samsara": 50, "Verkada": 55, "Faire": 55, "Gusto": 50, "Chime": 55,
    "Affirm": 55, "SoFi": 45, "Toast": 45, "Flexport": 45, "Asana": 55, "Airtable": 55, "Okta": 50, "Zscaler": 45,
    "Squarespace": 50, "Nextdoor": 50, "Quora": 55, "Sigma Computing": 50, "xAI": 60, "Mistral AI": 50,
    "Physical Intelligence": 60, "Cognition": 70, "Decagon": 55, "Abridge": 50, "Klaviyo": 45, "Whatnot": 55,
    "Canva": 45, "Lambda": 50, "Crusoe": 45, "Etched": 55, "Shield AI": 45, "Saronic": 45, "Hadrian": 40,
    "Vanta": 50, "Snyk": 45, "Tanium": 45, "Arctic Wolf": 40, "Riot Games": 45, "Epic Games": 45,
    "Chainalysis": 45, "Kraken": 50, "Gemini": 50, "Fireblocks": 45, "Wealthsimple": 40, "Marqeta": 45,
    "Attentive": 45, "Cockroach Labs": 50, "Temporal": 55, "Supabase": 45, "Hex": 50, "Watershed": 50,
    "Carta": 50, "Navan": 50, "Lattice": 45, "Grammarly": 50,
    # banks / finance / other known $40+ payers (SWE interns)
    "Capital One": 50, "Goldman Sachs": 45, "Morgan Stanley": 45, "JPMorgan Chase": 45, "JPMorgan": 45,
    "BlackRock": 45, "Visa": 50, "Mastercard": 42, "American Express": 42, "Fidelity Investments": 40,
    "CME Group": 45, "Nasdaq": 42, "Cboe": 45, "Citi": 42, "Bank of America": 42, "Wells Fargo": 40,
    "State Street": 35, "Activision-Blizzard": 40, "Electronic Arts": 40, "Sony": 40,
}


def table(cfg: dict) -> dict[str, float]:
    t = {norm(k): v for k, v in PAY_EST.items()}
    t.update({norm(k): float(v) for k, v in (cfg.get("pay_estimates") or {}).items()})
    return t


def estimate(company: str, tbl: dict[str, float]) -> float | None:
    return tbl.get(norm(company))
