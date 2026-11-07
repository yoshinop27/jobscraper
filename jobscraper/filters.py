"""Decide whether a posting is a US undergrad SWE/ML/data/quant internship for the target summer, in a target
city, and what it pays. Pure functions (no I/O) so they're easy to test."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

INTERN_RE = re.compile(r"\bintern(ship)?s?\b|\bco-?op\b|summer (analyst|associate|engineer|student)|"
                       r"\bstudent (researcher|engineer|developer)\b", re.I)

ROLE_RES = {
    "quant": re.compile(r"\bquant(itative)?\b|\btrad(er|ing)\b|\bstrateg(ist|y research)|market making|"
                        r"\balpha\b|\bresearch analyst\b", re.I),
    "ml": re.compile(r"machine learning|\bml\b|\bai\b|artificial intelligence|deep learning|\bllm|"
                     r"computer vision|\bnlp\b|research (engineer|scientist)|applied scien|\bperception\b|"
                     r"robot(ics)? learning|reinforcement", re.I),
    "data": re.compile(r"\bdata\b|analytics|\bstatistic|\bbusiness intelligence\b", re.I),
    "swe": re.compile(r"software|\bswe\b|\bsde\b|developer|engineer(ing)?\b|back[- ]?end|front[- ]?end|"
                      r"full[- ]?stack|infrastructure|platform|\bsystems?\b|\bsecurity\b|\bsre\b|devops|"
                      r"\bcloud\b|mobile|\bios\b|android|compiler|\bkernel\b|programm", re.I),
}
# Titles that match the role words above but aren't software/ML/data/quant work.
NON_ROLE_RE = re.compile(
    r"mechanical|electrical|hardware|manufactur|civil\b|chemical|structural|aerospace|propulsion|"
    r"\bsales\b|marketing|recruit|human resources|\bhr\b|\blegal\b|counsel|paralegal|accounting|\baudit|tax\b|"
    r"supply chain|procurement|facilities|construction|\bdesign(er)?\b(?! (verification|automation))|"
    r"product manag|program manag|project manag|\bux\b|content|communications|\bpr\b|"
    r"customer success|business (analyst|development|operations)|\bbiz ?ops\b|finance intern|"
    r"investment banking|wealth management|real estate|clinical|nurs|pharma|"
    r"\boperations\b|process engineer|quality (assurance )?engineer|\bfpga\b|\basic\b|\brf\b|analog|silicon|\bpcb\b|test technician|technician|field service|"
    r"electronics|\bmgr\b|\bmanager\b", re.I)  # "Intern Development Manager" = manages interns
GRAD_RE = re.compile(r"\bph\.?\s?d\b|doctoral|\bmba\b|master'?s|\bms\b(?!\s*office)|\bm\.s\.|postdoc|"
                     r"graduate (student|intern)|\bjd\b", re.I)
SEASON_RE = re.compile(r"\b(spring|fall|autumn|winter)\b", re.I)
YEAR_RE = re.compile(r"\b20(2[4-9]|3\d)\b")

CITIES = {
    "nyc": r"new york|\bnyc\b|manhattan|brooklyn|jersey city|hoboken",
    "sf": r"san francisco|\bsf\b|bay area|palo alto|mountain view|sunnyvale|san jose|menlo park|redwood city|"
          r"san mateo|santa clara|cupertino|foster city|south san francisco|oakland|berkeley|fremont|"
          r"san bruno|burlingame|emeryville|los gatos|milpitas|millbrae|belmont",
    "chicago": r"chicago|evanston",
    "austin": r"austin",
    "seattle": r"seattle|bellevue|redmond|kirkland|bothell",
    "boston": r"boston|cambridge,? ?(ma|massachusetts)|somerville|waltham|\bburlington,? ?ma\b",
}
REMOTE_RE = re.compile(r"\bremote\b|work from home|anywhere", re.I)
NON_US_RE = re.compile(
    r"\b(canada|toronto|vancouver|montreal|united kingdom|\buk\b|london|ireland|dublin|germany|berlin|munich|"
    r"france|paris|netherlands|amsterdam|switzerland|zurich|india|bangalore|bengaluru|hyderabad|pune|"
    r"singapore|hong kong|china|shanghai|beijing|japan|tokyo|korea|seoul|australia|sydney|israel|tel aviv|"
    r"poland|warsaw|spain|madrid|brazil|mexico|taiwan|emea|apac)\b", re.I)


@dataclass
class Verdict:
    keep: bool
    roles: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    remote: bool = False
    reasons: list[str] = field(default_factory=list)  # why it was dropped


def strip_html(s: str) -> str:
    """Tags -> spaces. Unescapes first because some APIs (Greenhouse) return escaped HTML."""
    s = html.unescape(s or "").replace("\\n", " ")
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


# A non-role word only disqualifies a title that doesn't also name the core work ("Hardware Software Intern").
_CORE_ROLE_RE = re.compile(r"software|machine learning|\bdata\b|quant", re.I)
_TERM_RE = re.compile(r"[a-z/ ]*(spring|summer|fall|winter)[a-z/ ]* 20\d\d")


def roles_of(title: str) -> list[str]:
    if NON_ROLE_RE.search(title) and not _CORE_ROLE_RE.search(title):
        return []
    return [r for r, rx in ROLE_RES.items() if rx.search(title)]


def season_ok(title: str, season: str = "Summer 2027", terms: list[str] | None = None) -> bool:
    """Reject titles/terms that clearly target another season or year; unspecified is fine."""
    want_season, want_year = season.split()
    # Placeholders like "N/A" mean unspecified, so fall through to the title.
    terms = [t for t in terms or [] if _TERM_RE.fullmatch(t.strip().lower())]
    if terms:
        return any(want_season.lower() in t.lower() and t.strip().endswith(want_year) for t in terms)
    return _title_season_ok(title, want_season, want_year)


def _title_season_ok(title: str, want_season: str, want_year: str) -> bool:
    years = set(YEAR_RE.findall(title))
    if years and want_year[2:] not in years:
        return False
    return not SEASON_RE.search(title) or want_season.lower() in title.lower()


def _undergrad_ok(title: str, degrees: list[str] | None) -> bool:
    return not GRAD_RE.search(title) and not (degrees and not any("bachelor" in d.lower() for d in degrees))


def _remote_us(text: str) -> bool:
    return bool(text) and bool(REMOTE_RE.search(text)) and not NON_US_RE.search(text)


def match_locations(locations: list[str], cities: list[str]) -> tuple[list[str], bool, bool]:
    """-> (matched city keys, remote_us, any_known). A location counts as known if it names a city/country."""
    text = " | ".join(locations)
    hit = [c for c in cities if re.search(CITIES[c], text, re.I)]
    return hit, _remote_us(text), bool(text.strip())


def judge(title: str, locations: list[str], cfg: dict, *, terms: list[str] | None = None,
          degrees: list[str] | None = None) -> Verdict:
    """Collects every failed check (not just the first) so the reasons show why a posting was dropped."""
    s = cfg["search"]
    v = Verdict(keep=False)
    if not INTERN_RE.search(title):
        v.reasons.append("not an internship")
    v.roles = [r for r in roles_of(title) if r in s["roles"]]
    if not v.roles:
        v.reasons.append("role")
    if not _undergrad_ok(title, degrees):
        v.reasons.append("grad-only")
    if not season_ok(title, s["season"], terms):
        v.reasons.append("season")
    v.cities, v.remote, known = match_locations(locations, s["cities"])
    if not _location_ok(v, known, s):
        v.reasons.append("location")
    v.keep = not v.reasons
    return v


def _location_ok(v: Verdict, known: bool, search: dict) -> bool:
    return bool(v.cities or (v.remote and search.get("include_remote"))
                or (not known and search.get("keep_unknown_location")))


# --------------------------------------------------------------------------- business internships
# A second, separate search: HR / marketing / accounting / finance / business-ops internships in the NYC, North Jersey
# and Boston areas. No pay or company-tier rules; it runs over every posting already stored (see serve.biz_jobs_json).
BIZ_ROLE_RES = {
    "hr": re.compile(r"human resources|\bhr\b|\bpeople\b|talent|recruit|\bpayroll\b|\bbenefits\b|workforce", re.I),
    "marketing": re.compile(r"marketing|\bbrand|social media|communications|\bcomms\b|public relations|\bpr\b|"
                            r"advertis|\bmedia\b|content|\bseo\b|copywrit|\bevents?\b|\bgrowth\b|creative", re.I),
    "accounting": re.compile(r"accounting|accountant|\baudit|\btax(ation)?\b|controller|bookkeep|\bsox\b|assurance", re.I),
    "finance": re.compile(r"financ|fp&a|treasury|\bcfo\b|credit|lending|banking|wealth|asset management|"
                          r"investment|insurance|underwrit|actuar|\brisk\b|economic|\bcapital markets\b", re.I),
    "business": re.compile(r"business|operations|\bops\b|strategy|\bsales\b|account (management|manager|executive)|"
                           r"customer (success|experience)|consult|supply chain|procurement|purchasing|merchandis|"
                           r"retail|real estate|project (management|coordinator)|administrative|office|compliance|"
                           r"\bgeneral management\b|leadership development", re.I),
}
# Software / research / engineering work belongs on the main Jobs tab, not here.
BIZ_TECH_RE = re.compile(r"software|engineer|developer|programm|machine learning|\bml\b|data scien|scientist|"
                         r"hardware|firmware|cyber|\bit\b (support|intern)|quantitative|\bquant\b", re.I)
# Front-office roles with very low acceptance rates; shown only when "all companies" is chosen.
BIZ_HARD_RE = re.compile(r"investment banking|\bib\b|sales\s*(and|&)\s*trading|\bs&t\b|private equity|\bm&a\b|mergers|"
                         r"venture capital|hedge fund|global markets|\btrad(er|ing)\b", re.I)
BIZ_SKIP_RE = re.compile(r"volunteer|unpaid", re.I)
BIZ_CITIES = {
    "nyc": r"new york|\bnyc\b|manhattan|brooklyn|queens|bronx|staten island|long island|white plains|westchester",
    "nj": r"new jersey|\bnj\b|jersey city|hoboken|newark|princeton|morristown|parsippany|iselin|edison|somerset|"
          r"basking ridge|bridgewater|florham park|whippany|piscataway|new brunswick|secaucus|paramus|montvale|"
          r"east hanover|berkeley heights|short hills|weehawken|fort lee|little falls|woodbridge|holmdel|mahwah",
    "boston": r"boston|cambridge,? ?(ma|massachusetts)|somerville|waltham|burlington,? ?(ma|massachusetts)|quincy|"
              r"woburn|needham|framingham|braintree|wellesley|marlborough|andover|newton,? ?(ma|massachusetts)|"
              r"lexington,? ?(ma|massachusetts)|\bmassachusetts\b",
}


@dataclass
class BizVerdict:
    keep: bool
    roles: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    remote: bool = False
    hard: bool = False  # front-office (IB, S&T, PE, ...): technically a business role, but very hard to land


def judge_business(title: str, locations: list[str], *, season: str = "Summer 2027", terms: list[str] | None = None,
                   degrees: list[str] | None = None) -> BizVerdict:
    v = BizVerdict(keep=False)
    if not _biz_eligible(title, season, terms, degrees):
        return v
    v.roles = [r for r, rx in BIZ_ROLE_RES.items() if rx.search(title)]
    text = " | ".join(locations)
    v.cities = [c for c, rx in BIZ_CITIES.items() if re.search(rx, text, re.I)]
    v.remote = _remote_us(text)
    v.hard = bool(BIZ_HARD_RE.search(title))
    v.keep = bool(v.roles) and bool(v.cities or v.remote)
    return v


def _biz_eligible(title: str, season: str, terms: list[str] | None, degrees: list[str] | None) -> bool:
    return (bool(INTERN_RE.search(title)) and not BIZ_SKIP_RE.search(title) and not BIZ_TECH_RE.search(title)
            and _undergrad_ok(title, degrees) and season_ok(title, season, terms))


# --------------------------------------------------------------------------- pay
_NUM = r"\$\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?(k|K)?"
PAY_RE = re.compile(_NUM + r"(?!\s?(?:million|billion|trillion|[mb]n?)\b)(?:\s?(?:-|–|—|to)\s?" + _NUM + r")?\s?(?:(?:USD|usd)\s?)?"
                    r"(?:/|\bper\b|\ban\b|\ba\b)?\s?(hour|hr|hourly|week|wk|month|mo|monthly|year|yr|annual|annually)?",
                    re.I)
PAY_CONTEXT = re.compile(r"salary|pay|compensation|wage|hourly|stipend|rate|base|range|zone|earn", re.I)
PER_HOUR = {"hour": 1, "hr": 1, "hourly": 1, "week": 40, "wk": 40, "month": 173.33, "mo": 173.33,
            "monthly": 173.33, "year": 2080, "yr": 2080, "annual": 2080, "annually": 2080}


def _num(n: str, k: str | None) -> float:
    return float(n.replace(",", "")) * (1000 if k else 1)


def parse_hourly(text: str) -> tuple[float, float] | None:
    """Best-effort pay range in $/hr from free text. Unit-less amounts are classified by size:
    <= 300 hourly, <= 20k monthly, >= 30k annual."""
    best = None
    for m in PAY_RE.finditer(text or ""):
        cand = _hourly_range(m, text)
        if cand and (best is None or (cand[2] and not best[2])):  # first match wins, unless a later one has a unit
            best = cand
    return best[:2] if best else None


def _hourly_range(m: re.Match, text: str) -> tuple[float, float, bool] | None:
    """-> (lo $/hr, hi $/hr, had explicit unit) for one PAY_RE match, or None if it isn't plausible pay."""
    lo = _num(m.group(1), m.group(2))
    hi = _num(m.group(3), m.group(4)) if m.group(3) else lo
    unit = (m.group(5) or "").lower()
    if not unit and not PAY_CONTEXT.search(text[max(0, m.start() - 120):m.start()]):
        return None  # a bare dollar amount far from any pay wording (funding, revenue, ...)
    div = PER_HOUR[unit] if unit else _divisor_by_size(lo)
    if div is None:
        return None
    lo_h, hi_h = lo / div, hi / div
    if not (12 <= lo_h <= 400) or hi_h < lo_h:
        return None
    return round(lo_h, 2), round(hi_h, 2), bool(unit)


def _divisor_by_size(amount: float) -> float | None:
    if 15 <= amount <= 300:
        return 1
    if 2000 <= amount <= 20000:
        return 173.33
    if amount >= 30000:
        return 2080
    return None
