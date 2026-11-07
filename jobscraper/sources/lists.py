"""Community internship lists on GitHub. Used two ways:
1. as a source (catches companies whose sites we can't poll directly: Meta, Apple, Citadel, ...);
2. as the benchmark we're trying to beat: when a list adds a posting we already had, we record how much earlier
   we saw it.
They're also mined for board discovery (which ATS + board each company uses), see companies.py.
"""
from __future__ import annotations

import re

from jobscraper.http import get_json, get_text

SIMPLIFY = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
VANSH = "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json"
SPEEDY = "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/README.md"


def _posting(source: str, company: str, job_id: str, title: str, locations: list, url: str | None, *,
             posted_at=None, terms=None, degrees=None, description=None) -> dict:
    return {"source": source, "company": company, "job_id": job_id, "title": title, "locations": locations,
            "url": url, "posted_at": posted_at, "terms": terms, "degrees": degrees, "description": description}


# --------------------------------------------------------------------------- listings.json (Simplify format)
def _from_listings(rows: list[dict], source: str) -> list[dict]:
    return [_listing_posting(r, source) for r in rows if r.get("active", True) and r.get("is_visible", True)]


def _listing_posting(r: dict, source: str) -> dict:
    terms = r.get("terms") or ([f"{r['season']} 2027"] if r.get("season") else None)
    return _posting(source, r.get("company_name", "").strip(), r["id"], r.get("title", ""),
                    r.get("locations") or [], r.get("url"), posted_at=r.get("date_posted"), terms=terms,
                    degrees=r.get("degrees"))


def simplify() -> list[dict]:
    return _from_listings(get_json(SIMPLIFY, timeout=60), "simplify")


def vansh() -> list[dict]:
    return _from_listings(get_json(VANSH, timeout=60), "vanshb03")


# --------------------------------------------------------------------------- speedyapply README (markdown table)
_HEADING = re.compile(r"^#{2,4}\s*(.*)")
# Table row: company | title | location | extra (kept as description) | link cell | age in days
_ROW = re.compile(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.*?)\s*\|\s*(.+?)\s*\|\s*(\d+)d\s*\|", re.M)
_TAG = re.compile(r"<[^>]+>")
_HREF = re.compile(r'href="([^"]+)"')


def speedy() -> tuple[list[dict], dict[str, str]]:
    """-> (postings, {company: "faang"|"quant"}) from speedyapply's README, which groups companies by tier."""
    out, tiers, section = [], {}, None
    for line in get_text(SPEEDY, timeout=60).splitlines():
        if h := _HEADING.match(line):
            section = _speedy_section(h.group(1), section)
            continue
        m = _ROW.match(line)
        if not m or m.group(1) == "Company":
            continue
        posting = _speedy_posting(m)
        if section in ("faang", "quant"):
            tiers[posting["company"]] = section
        out.append(posting)
    return out, tiers


def _speedy_section(heading: str, current: str | None) -> str | None:
    """Tier named by a heading; headings that name no tier keep the current one."""
    t = heading.lower()
    return "faang" if "faang" in t else "quant" if "quant" in t else "other" if "other" in t else current


def _speedy_posting(m: re.Match) -> dict:
    url = _HREF.search(m.group(5))
    return _posting("speedyapply", _TAG.sub("", m.group(1)).replace("**", "").strip(),
                    url.group(1) if url else m.group(2), _TAG.sub("", m.group(2)).strip(),
                    [_TAG.sub("", m.group(3))], url.group(1) if url else None, description=m.group(4))


LISTS = {"simplify": simplify, "vanshb03": vansh}
