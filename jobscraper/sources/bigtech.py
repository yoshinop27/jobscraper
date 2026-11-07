"""Companies with their own careers APIs (not on a shared ATS). Each returns postings like sources/ats.py.

Covered: Amazon, Google, TikTok, Netflix, Microsoft. Not covered directly (bot protection or login walls):
Meta, Apple, Citadel, Tesla, Goldman, Two Sigma, SIG — those come in through the Simplify/GitHub lists.

Every API here is paginated the same way: `_<company>_page(start) -> (raw jobs, more pages?)` feeds `_paginate`,
which maps each raw job through `_<company>_posting`.
"""
from __future__ import annotations

import json
import re
import time
from typing import Callable

from jobscraper.filters import strip_html
from jobscraper.http import get_json, get_text, post_json


def _paginate(fetch_page: Callable[[int], tuple[list, bool]], starts: range,
              to_posting: Callable[[object], dict]) -> list[dict]:
    out = []
    for start in starts:
        jobs, more = fetch_page(start)
        out += [to_posting(j) for j in jobs]
        if not more:
            break
    return out


def _posting(job_id: str, title: str, locations: list, url: str, posted_at, description: str | None) -> dict:
    return {"job_id": job_id, "title": title, "locations": locations, "url": url, "posted_at": posted_at,
            "description": description}


# --------------------------------------------------------------------------- Amazon
def amazon(b: dict) -> list[dict]:
    return _paginate(_amazon_page, range(0, 500, 100), _amazon_posting)


def _amazon_page(offset: int) -> tuple[list, bool]:
    d = get_json("https://www.amazon.jobs/en/search.json?base_query=intern&result_limit=100&sort=recent"
                 f"&offset={offset}&normalized_country_code%5B%5D=USA")
    jobs = d.get("jobs") or []
    return jobs, len(jobs) >= 100


def _amazon_posting(j: dict) -> dict:
    desc = " ".join(strip_html(j.get(k) or "") for k in ("description", "basic_qualifications",
                                                           "preferred_qualifications"))
    return _posting(str(j.get("id_icims") or j.get("id")), j.get("title", ""),
                    [j.get("normalized_location") or j.get("location") or ""],
                    "https://www.amazon.jobs" + j.get("job_path", ""), _amazon_date(j.get("posted_date", "")), desc)


def _amazon_date(s: str) -> float | None:
    try:
        return time.mktime(time.strptime(s, "%B %d, %Y"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- Google
# The results page embeds its data as a JS literal; there is no public JSON endpoint.
_GOOGLE_DATA = re.compile(r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(.*?), sideChannel", re.S)


def google(b: dict) -> list[dict]:
    return _paginate(_google_page, range(1, 11), _google_posting)


def _google_page(page: int) -> tuple[list, bool]:
    h = get_text("https://www.google.com/about/careers/applications/jobs/results"
                 f"?q=intern&target_level=INTERN_AND_APPRENTICE&sort_by=date&page={page}")
    m = _GOOGLE_DATA.search(h)
    if not m:
        return [], False
    data = json.loads(m.group(1))
    jobs = data[0] or []
    total = data[2] if len(data) > 2 and isinstance(data[2], int) else 0
    return jobs, bool(jobs) and page * 20 < total


def _google_posting(j: list) -> dict:
    """Jobs are positional arrays: [0] id, [1] title, [9] locations, [12] posted, the rest description sections."""
    locs = [l[0] for l in (j[9] or []) if l]
    desc = " ".join(strip_html((x or [None, ""])[1] or "") for x in (j[3], j[4], j[10], j[18], j[19])
                    if isinstance(x, list))
    return _posting(str(j[0]), j[1], locs, f"https://www.google.com/about/careers/applications/jobs/results/{j[0]}",
                    (j[12] or [None])[0], desc)


# --------------------------------------------------------------------------- TikTok
def tiktok(b: dict) -> list[dict]:
    return _paginate(_tiktok_page, range(0, 600, 100), _tiktok_posting)


def _tiktok_page(offset: int) -> tuple[list, bool]:
    d = post_json("https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts",
                  {"recruitment_id_list": [], "job_category_id_list": [], "keyword": "intern",
                   "location_code_list": [], "limit": 100, "offset": offset},
                  headers={"website-path": "tiktok", "Origin": "https://lifeattiktok.com",
                           "Referer": "https://lifeattiktok.com/"})
    posts = (d.get("data") or {}).get("job_post_list") or []
    return posts, len(posts) >= 100


def _tiktok_posting(j: dict) -> dict:
    city = j.get("city_info") or {}
    loc = city.get("en_name") or ""
    region = (city.get("parent") or {}).get("parent") or {}
    if region.get("en_name"):
        loc += f", {region['en_name']}"
    return _posting(str(j["id"]), j.get("title", ""), [loc], f"https://lifeattiktok.com/search/{j['id']}", None,
                    f"{j.get('description', '')} {j.get('requirement', '')}")


# --------------------------------------------------------------------------- Eightfold (Netflix)
def _eightfold(domain: str, host: str, query: str = "intern") -> list[dict]:
    def page(start: int) -> tuple[list, bool]:
        d = get_json(f"https://{host}/api/apply/v2/jobs?domain={domain}&query={query}&start={start}&num=10"
                     "&sort_by=timestamp")
        pos = d.get("positions") or []
        return pos, len(pos) >= 10 and start + 10 < (d.get("count") or 0)

    def to_posting(p: dict) -> dict:
        return _posting(str(p["id"]), p.get("name", ""), p.get("locations") or [p.get("location") or ""],
                        p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{p['id']}",
                        p.get("t_create"), strip_html(p.get("job_description") or ""))

    return _paginate(page, range(0, 300, 10), to_posting)


def netflix(b: dict) -> list[dict]:
    return _eightfold("netflix.com", "explore.jobs.netflix.net")


# --------------------------------------------------------------------------- Microsoft
def microsoft(b: dict) -> list[dict]:
    return _paginate(_microsoft_page, range(0, 300, 10), _microsoft_posting)


def _microsoft_page(start: int) -> tuple[list, bool]:
    d = get_json("https://apply.careers.microsoft.com/api/pcsx/search?domain=microsoft.com&query=intern"
                 f"&location=United%20States&start={start}&num=10&sort_by=timestamp")
    pos = (d.get("data") or d).get("positions") or []
    return pos, len(pos) >= 10


def _microsoft_posting(p: dict) -> dict:
    return _posting(str(p.get("id")), p.get("name", ""), p.get("standardizedLocations") or p.get("locations") or [],
                    f"https://apply.careers.microsoft.com/careers/job/{p.get('id')}",
                    p.get("postedTs") or p.get("creationTs"), None)


FETCH = {"amazon": amazon, "google": google, "tiktok": tiktok, "netflix": netflix, "microsoft": microsoft}

BOARDS = [
    {"ats": "amazon", "slug": "amazon", "company": "Amazon"},
    {"ats": "google", "slug": "google", "company": "Google"},
    {"ats": "tiktok", "slug": "tiktok", "company": "TikTok"},
    {"ats": "netflix", "slug": "netflix", "company": "Netflix"},
    {"ats": "microsoft", "slug": "microsoft", "company": "Microsoft"},
]
