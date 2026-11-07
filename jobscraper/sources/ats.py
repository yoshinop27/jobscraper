"""Public job-board APIs used by most companies: Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Oracle.

Each fetcher takes a board spec (dict from data/boards.json), makes the listing request(s), and maps every raw job
through a `_<ats>_posting` normalizer into:
  {"job_id", "title", "locations": [str], "url", "posted_at": epoch|None, "description": str|None}
`detail_*` functions fetch the full description (for pay) and are only called for new postings that already
passed the title/location filters, so a poll costs one request per board.
"""
from __future__ import annotations

import concurrent.futures as cf
import re
import time
from datetime import datetime
from urllib.parse import quote

from jobscraper.filters import INTERN_RE, strip_html
from jobscraper.http import get_json, post_json


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _posting(job_id, title, locations, url, posted_at, description=None, **extra) -> dict:
    return {"job_id": job_id, "title": title, "locations": locations, "url": url, "posted_at": posted_at,
            "description": description, **extra}


def _nonempty(items: list) -> list:
    return [x for x in items if x]


def _pages(first, fetch_page, offsets: list[int], workers: int = 6) -> list:
    """First page already fetched; fetch the remaining page offsets in parallel (pages were the slow part)."""
    if not offsets:
        return [first]
    with cf.ThreadPoolExecutor(min(workers, len(offsets))) as ex:
        return [first, *ex.map(fetch_page, offsets)]


# --------------------------------------------------------------------------- Greenhouse
def greenhouse(b: dict) -> list[dict]:
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{b['slug']}/jobs")
    return [_greenhouse_posting(j) for j in d.get("jobs", [])]


def _greenhouse_posting(j: dict) -> dict:
    locs = [(j.get("location") or {}).get("name") or ""]
    locs += [o.get("name", "") for o in j.get("offices", []) or []]
    return _posting(str(j["id"]), j.get("title", ""), _nonempty(locs), j.get("absolute_url"),
                    _ts(j.get("first_published") or j.get("updated_at")))


def detail_greenhouse(b: dict, job: dict) -> str:
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{b['slug']}/jobs/{job['job_id']}?pay_transparency=true")
    return _greenhouse_pay(d.get("pay_input_ranges") or []) + strip_html(d.get("content", ""))


def _greenhouse_pay(ranges: list[dict]) -> str:
    """Structured pay, when the company fills it in, as a sentence parse_hourly understands."""
    for r in ranges:
        lo, hi = (r.get("min_cents") or 0) / 100, (r.get("max_cents") or 0) / 100
        if lo and (r.get("currency_type") or "USD") == "USD":
            unit = "hour" if hi <= 300 else "year" if lo >= 20000 else "month"
            return f"Pay range: ${lo:,.2f} - ${hi or lo:,.2f} per {unit}. "
    return ""


# --------------------------------------------------------------------------- Lever
def lever(b: dict) -> list[dict]:
    host = "api.eu.lever.co" if b.get("eu") else "api.lever.co"
    d = get_json(f"https://{host}/v0/postings/{b['slug']}?mode=json")
    return [_lever_posting(j) for j in d] if isinstance(d, list) else []


def _lever_posting(j: dict) -> dict:
    cat = j.get("categories") or {}
    locs = cat.get("allLocations") or [cat.get("location") or ""]
    return _posting(j["id"], j.get("text", ""), _nonempty(locs), j.get("hostedUrl"),
                    (j.get("createdAt") or 0) / 1000 or None, _lever_description(j))


def _lever_description(j: dict) -> str:
    lists = " ".join(strip_html(x.get("content", "")) for x in j.get("lists", []) or [])
    desc = f"{j.get('descriptionPlain') or ''} {lists} {j.get('additionalPlain') or ''}"
    sr = j.get("salaryRange") or {}
    if sr.get("min"):
        desc += f" ${sr['min']} - ${sr.get('max', sr['min'])} per {str(sr.get('interval', '')).replace('per-', '')}"
    return desc


# --------------------------------------------------------------------------- Ashby
def ashby(b: dict) -> list[dict]:
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{b['slug']}?includeCompensation=true")
    return [_ashby_posting(j) for j in d.get("jobs", []) if j.get("isListed") is not False]


def _ashby_posting(j: dict) -> dict:
    comp = j.get("compensation") or {}
    pay = comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary") or ""
    return _posting(j["id"], j.get("title", ""), _nonempty(_ashby_locations(j)), j.get("jobUrl"),
                    _ts(j.get("publishedAt")), f"{pay} {j.get('descriptionPlain') or ''}")


def _ashby_locations(j: dict) -> list[str]:
    locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
    addr = (j.get("address") or {}).get("postalAddress") or {}
    if addr.get("addressLocality"):
        locs.append(f"{addr.get('addressLocality')}, {addr.get('addressRegion', '')}")
    if j.get("isRemote"):
        locs.append("Remote")
    return locs


# --------------------------------------------------------------------------- Workday
_POSTED = re.compile(r"posted (today|yesterday|(\d+)\+? days? ago)", re.I)


def _wd_base(b: dict) -> str:
    return f"https://{b['host']}/wday/cxs/{b['tenant']}/{b['site']}"


def _wd_search(b: dict, offset: int, text: str) -> dict:
    return post_json(_wd_base(b) + "/jobs", {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": text})


def _wd_days(posted_on: str | None) -> int | None:
    m = _POSTED.search(posted_on or "")
    if not m:
        return None
    word = m.group(1).lower()
    return 0 if word == "today" else 1 if word == "yesterday" else int(m.group(2)) if m.group(2) else None


def workday(b: dict) -> list[dict]:
    """Two modes. Full (b["_incremental"] false): search 'intern' across all result pages, fetched in parallel;
    this is the complete list and is used to detect closed postings. Incremental: the unfiltered listing is sorted
    newest first, so read pages only until postings are 2+ days old (usually one request). Workday rate-limits
    per client IP, so the incremental mode is what keeps frequent checks fast."""
    pages = _wd_recent_pages(b) if b.get("_incremental") else _wd_all_intern_pages(b)
    return [_workday_posting(b, j) for d in pages for j in d.get("jobPostings") or []
            if INTERN_RE.search(j.get("title", ""))]


def _wd_age(p: dict) -> int:
    """Days since a Workday posting went up; unparseable dates count as old. ("Posted Today" is 0, not falsy-old.)"""
    days = _wd_days(p.get("postedOn"))
    return 99 if days is None else days


def _wd_recent_pages(b: dict) -> list[dict]:
    pages = []
    for offset in range(0, 100, 20):
        d = _wd_search(b, offset, "")
        pages.append(d)
        posts = d.get("jobPostings") or []
        if not posts or any(_wd_age(p) >= 2 for p in posts):
            break
    return pages


def _wd_all_intern_pages(b: dict) -> list[dict]:
    first = _wd_search(b, 0, "intern")
    offsets = list(range(20, min(first.get("total") or 0, 400), 20))
    return _pages(first, lambda offset: _wd_search(b, offset, "intern"), offsets, workers=4)


def _workday_posting(b: dict, j: dict) -> dict:
    days = _wd_days(j.get("postedOn"))
    path = j.get("externalPath", "")
    return _posting(path.rsplit("_", 1)[-1] or path, j.get("title", ""), [j.get("locationsText") or ""],
                    f"https://{b['host']}/{b.get('url_prefix', '')}{b['site']}{path}",
                    time.time() - days * 86400 if days is not None else None, _path=path)


def detail_workday(b: dict, job: dict) -> str:
    """Also appends the extra locations, which only the detail endpoint lists, to job["locations"]."""
    d = get_json(_wd_base(b) + job["_path"]) if job.get("_path") else {}
    info = d.get("jobPostingInfo") or {}
    extra = " ".join(info.get("additionalLocations") or [])
    if extra:
        job["locations"] = job["locations"] + [extra]
    return strip_html(info.get("jobDescription", "")) + " " + extra


# --------------------------------------------------------------------------- SmartRecruiters
def smartrecruiters(b: dict) -> list[dict]:
    d = get_json(f"https://api.smartrecruiters.com/v1/companies/{quote(b['slug'])}/postings?q=intern&limit=100")
    return [_smartrecruiters_posting(b, j) for j in d.get("content", [])]


def _smartrecruiters_posting(b: dict, j: dict) -> dict:
    loc = j.get("location") or {}
    locs = [", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)]
    if loc.get("remote"):
        locs.append("Remote")
    return _posting(j["id"], j.get("name", ""), locs, f"https://jobs.smartrecruiters.com/{b['slug']}/{j['id']}",
                    _ts(j.get("releasedDate")), _ref=j.get("ref"))


def detail_smartrecruiters(b: dict, job: dict) -> str:
    d = get_json(job["_ref"]) if job.get("_ref") else {}
    sections = (d.get("jobAd") or {}).get("sections") or {}
    return " ".join(strip_html((sections.get(k) or {}).get("text", "")) for k in
                    ("jobDescription", "qualifications", "additionalInformation"))


# --------------------------------------------------------------------------- Oracle Recruiting Cloud (HCM)
def oracle(b: dict) -> list[dict]:
    first = _oracle_page(b, 0)
    total = min(_oracle_result(first).get("TotalJobsCount") or 0, 500)
    pages = _pages(first, lambda offset: _oracle_page(b, offset), list(range(100, total, 100)))
    return [_oracle_posting(b, j) for d in pages for j in _oracle_result(d).get("requisitionList") or []]


def _oracle_page(b: dict, offset: int) -> dict:
    return get_json(
        f"https://{b['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
        f"&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={b['site']},"
        f"keyword=intern,limit=100,offset={offset},sortBy=POSTING_DATES_DESC")


def _oracle_result(d: dict) -> dict:
    return (d.get("items") or [{}])[0]


def _oracle_posting(b: dict, j: dict) -> dict:
    locs = [j.get("PrimaryLocation") or ""] + [s.get("Name", "") for s in j.get("secondaryLocations") or []]
    posted = j.get("PostedDate")
    return _posting(str(j["Id"]), j.get("Title", ""), _nonempty(locs),
                    f"https://{b['host']}/hcmUI/CandidateExperience/en/sites/{b['site']}/job/{j['Id']}",
                    _ts(posted + "T00:00:00+00:00" if posted else None), j.get("ShortDescriptionStr") or "")


FETCH = {"greenhouse": greenhouse, "oracle": oracle, "lever": lever, "ashby": ashby, "workday": workday,
         "smartrecruiters": smartrecruiters}
DETAIL = {"greenhouse": detail_greenhouse, "workday": detail_workday, "smartrecruiters": detail_smartrecruiters}
