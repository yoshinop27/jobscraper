"""Curated high-priority companies: prestige and/or $40+/hr SWE/ML/data/quant intern pay.

Tiered companies are polled every `fast_minutes` and shown even when a posting lists no pay. Each name is matched
against job-board company names via companies.norm(); aliases go on the same line separated by '='.
Add more in config.toml [tiers] without editing this file.
"""

WATCHLIST: dict[str, str] = {
    # Big tech and top public/late-stage tech
    "faang": """
Google = Alphabet = Google DeepMind = DeepMind | Meta = Facebook | Amazon = Amazon Web Services = AWS | Apple
Netflix | Microsoft = GitHub | NVIDIA | Uber | Airbnb | Stripe | Databricks | Snowflake | Salesforce | Adobe
LinkedIn | Bloomberg | Palantir | OpenAI | Anthropic | Coinbase | Robinhood | Pinterest | Snap | Lyft | DoorDash
Instacart | Roblox | Dropbox | Figma | Datadog | MongoDB | Cloudflare | Atlassian | Block = Square | Twilio
Duolingo | Discord | Reddit | Spotify | Waymo | Tesla | SpaceX | Oracle | Intuit | ServiceNow | Qualcomm | AMD
TikTok = ByteDance | Shopify | Pure Storage | Nuro | Zoox | Scale AI | Ramp | Plaid | Brex | Notion | Rippling
X Development | Intel | Broadcom | Workday | Autodesk | Splunk | HubSpot | Zoom | DocuSign | Box | Okta | Twitch
Yelp | Etsy | eBay | PayPal | Wayfair | Zillow | Expedia Group | Booking Holdings | Roku | Unity | Electronic Arts
Epic Games | Riot Games | Activision Blizzard | Valve | Niantic | Micron | Marvell | Synopsys | Cadence Design Systems
KLA | Lam Research | Applied Materials | Texas Instruments | Arista Networks | Cisco | Palo Alto Networks
CrowdStrike | Zscaler | Fortinet | Elastic | Confluent | GitLab | HashiCorp | Samsara | Toast | Affirm | SoFi
Chime | Gusto | Asana | Airtable | Canva | Grammarly | Quora | Nextdoor | Squarespace | Peloton | Lucid Motors
Rivian | Aurora Innovation | Motional | Kodiak Robotics | Applied Intuition | Anduril | Flexport | Faire | Gopuff
""",
    # Quant trading, hedge funds, market makers
    "quant": """
Jane Street | Citadel | Citadel Securities | Hudson River Trading = HRT | Jump Trading | Two Sigma
D. E. Shaw = DE Shaw = D.E. Shaw | Optiver | IMC Trading = IMC | Susquehanna = SIG | DRW | Five Rings
Tower Research Capital | Akuna Capital | Old Mission | Belvedere Trading | Radix Trading | XTX Markets | Millennium
Point72 | Cubist | Bridgewater | AQR | Headlands Technologies = Headlands Tech Holdings | Chicago Trading Company
PEAK6 | Wolverine Trading | Geneva Trading | Squarepoint = Squarepoint Capital | Voleon | Arrowstreet = Arrowstreet Capital
PDT Partners | Qube Research & Technologies = Qube | Vatic Labs | Engineers Gate | Quadrature | Garda Capital Partners
Valkyrie Trading | Clear Street | Flow Traders | Da Vinci | Graviton Research Capital | Mako Trading | Eagle Seven
Hehmeyer | Balyasny | Schonfeld | Tradeweb | Group One Trading | Maven Securities | Renaissance Technologies
WorldQuant | Man Group | Marshall Wace | G-Research | DV Trading | HAP Capital | ExodusPoint | Verition
Hudson Bay Capital | Capula | Arcesium | Virtu | Optiver US | Jump Crypto | Wintermute | GSR | Galaxy Digital
""",
    # Top private tech / AI labs / high-paying startups
    "unicorn": """
xAI | Mistral AI | Cohere | Perplexity | Hugging Face | Character.AI = Character | Anysphere = Cursor | Cognition
Replit | Glean | Harvey | Sierra | Decagon | Hebbia | Abridge | Writer | Weights & Biases | Pinecone | Snorkel AI
Labelbox | Together AI | Fireworks AI | Baseten | Modal | Groq | Cerebras | SambaNova | Lambda | CoreWeave | Crusoe
Etched | Tenstorrent | Lightmatter | Figure AI | Physical Intelligence | Skild AI | 1X | Runway | ElevenLabs
Stability AI | Vercel | Netlify | Supabase | Neon | PlanetScale | Cockroach Labs | Temporal | Postman | Sourcegraph
Linear | Retool | Miro | ClickUp | Fivetran | dbt Labs | Starburst | ClickHouse | Redis | Wiz | Snyk | Tanium
SentinelOne | Abnormal Security | Vanta | Drata | 1Password | Arctic Wolf | Rubrik | Cohesity | Netskope | Verkada
Flock Safety | Shield AI | Saronic | Hadrian | Relativity Space | Neuralink | Benchling | Tempus | Flatiron Health
Oscar Health | Whatnot | Navan | Deel | Lattice | Klaviyo | Attentive | Braze | Amplitude | Mercury | Wealthfront
Carta | Modern Treasury | Marqeta | Kraken | Gemini | Circle | Ripple | Chainalysis | Fireblocks | Alchemy
Webflow | Benchling | Sigma Computing | Hex | Watershed | Rippling | Mercor | Clay | Anduril | Scale AI | Ramp
Notion | Plaid | Brex | Faire | Samsara | Applied Intuition | Waabi | Zipline | Wayve | Bilt Rewards | Nubank
""",
    # Banks, payments, exchanges and others known for $40+/hr tech interns
    "highpay": """
Capital One | Goldman Sachs | Morgan Stanley | JPMorgan Chase = JPMorgan = J.P. Morgan | BlackRock | Visa
Mastercard | American Express | Fidelity Investments | CME Group | Nasdaq | Cboe | Intercontinental Exchange = ICE
Citi | Bank of America | Wells Fargo
Barclays | Jefferies | Evercore | Sony = Sony Interactive Entertainment = PlayStation | Nintendo | VMware | Toyota Research Institute
""",
}


def _groups(block: str) -> list[list[str]]:
    """'A = A2 | B' (lines also separate entries) -> [['A', 'A2'], ['B']]"""
    groups = []
    for item in block.replace("\n", "|").split("|"):
        names = [n.strip() for n in item.split("=") if n.strip()]
        if names:
            groups.append(names)
    return groups


def parse() -> dict[str, list[list[str]]]:
    """tier -> [[canonical name, alias, ...], ...]"""
    return {tier: _groups(block) for tier, block in WATCHLIST.items()}
