"""Poll company boards and community lists, record postings, alert on new matches.

A board is due every `fast_minutes` (tiered companies) or `slow_minutes` (everyone else). A board's first
successful poll is a silent baseline, so alerts only fire for postings that appear after we started watching.
"""
from __future__ import annotations

import concurrent.futures as cf
import collections
import json
import time

from jobscraper import db, notify, pay as paymod
from jobscraper.companies import load_boards, norm, tier_of, tier_sets
from jobscraper.filters import judge, parse_hourly
from jobscraper.sources import ats, bigtech, lists

FETCH = {**ats.FETCH, **bigtech.FETCH}
TIERED = ("faang", "quant", "unicorn", "highpay")
LANES = {"fast": lambda b: b["tier"] in TIERED, "slow": lambda b: b["tier"] not in TIERED, "all": lambda b: True}

FULL_EVERY = 6 * 3600  # incremental boards (Workday) get a complete listing this often, to detect closed postings
INCREMENTAL_ATS = {"workday"}
BROKEN_FAILS = 5
BROKEN_RETRY = 6 * 3600  # broken/renamed board: retry a few times a day, not every pass or on "Check now"

# A pass waits this long for its boards, then moves on; slower boards keep downloading in the background and are
# saved by a later pass, so one slow careers site never delays everyone else's next check.
PASS_WAIT = 25
_pool: cf.ThreadPoolExecutor | None = None
_inflight: dict[str, cf.Future] = {}  # board key -> fetch still running from this or an earlier pass

_PAY_CACHE: dict = {}


def _fmt_pay(lo, hi, est=None) -> str:
    if lo is None:
        return f"~${est:.0f}/hr est." if est else ""
    return f"${lo:.0f}/hr" if not hi or abs(hi - lo) < 1 else f"${lo:.0f}-{hi:.0f}/hr"


def evaluate(cfg: dict, tier: str, title: str, locations: list[str], description: str | None,
             terms=None, degrees=None, company: str = "", pay: tuple | None = None) -> dict:
    """Filter verdict + pay + company rule -> fields stored on the job row.
    Pay: the posted range when the description has one, else the company's reputation estimate (pay.py)."""
    v = judge(title, locations, cfg, terms=terms, degrees=degrees)
    pay = pay or parse_hourly(description or "")
    est = None if pay else paymod.estimate(company, _pay_table(cfg))
    s = cfg["search"]
    reasons = list(v.reasons)
    mid = (pay[0] + pay[1]) / 2 if pay else None  # a $22-41 range doesn't count as a $40/hr job
    if pay and mid < s["min_hourly"]:
        reasons.append("pay")
    if _fails_company_rule(s, tier, mid if pay else est):
        reasons.append("company")
    return {"matched": int(not reasons), "roles": ",".join(v.roles), "cities": ",".join(v.cities),
            "remote": int(v.remote), "reasons": ",".join(reasons),
            "pay_lo": pay[0] if pay else None, "pay_hi": pay[1] if pay else None, "pay_est": est}


def _fails_company_rule(search: dict, tier: str, best_pay: float | None) -> bool:
    """Non-priority companies only count when config allows it (by default: when their pay clears the bar)."""
    if tier in TIERED:
        return False
    rule = search.get("other_companies", "if_pay_known")
    return rule == "none" or (rule == "if_pay_known" and not (best_pay and best_pay >= search["min_hourly"]))


def _pay_table(cfg: dict) -> dict:
    key = repr(sorted((cfg.get("pay_estimates") or {}).items()))
    if key not in _PAY_CACHE:
        _PAY_CACHE.clear()
        _PAY_CACHE[key] = paymod.table(cfg)
    return _PAY_CACHE[key]


def _match_keys(company: str, title: str, url: str | None) -> tuple[str, str]:
    """(url_key, title_key): how a board posting and a list posting are recognized as the same job."""
    return db.url_key(url), db.title_key(norm(company), title)


def _alert(key: str, company: str, title: str, url: str | None, loc: str, fields: dict) -> dict:
    return {"company": company, "title": title, "url": url, "loc": loc,
            "pay": _fmt_pay(fields["pay_lo"], fields["pay_hi"], fields["pay_est"]), "key": key}


def _insert_job(con, row: dict, ignore: bool = False) -> bool:
    """Insert a jobs row given as {column: value}; returns whether a row was actually added."""
    sql = (f"INSERT {'OR IGNORE ' if ignore else ''}INTO jobs({', '.join(row)}) "
           f"VALUES({', '.join('?' * len(row))})")
    return con.execute(sql, tuple(row.values())).rowcount == 1


def dedupe(con) -> int:
    """Collapse open postings that are the same job listed on two of a company's boards (e.g. Invesco's main and
    early-careers Workday sites): same company+title and same locations. Keeps the one you've acted on
    (applied/hidden), else the first found; the rest get status 'dup'."""
    groups = collections.defaultdict(list)
    for r in con.execute("SELECT key, board, title_key, locations, status, first_seen FROM jobs WHERE source='board' "
                         "AND closed_at IS NULL AND status != 'dup' AND matched=1"):
        groups[(r["title_key"], r["locations"])].append(r)
    rank = {"applied": 0, "hidden": 1}
    dups = []
    for rows in groups.values():
        if len({r["board"] for r in rows}) < 2:
            continue  # several openings on one board are separate requisitions; leave them
        rows.sort(key=lambda r: (rank.get(r["status"], 2), r["first_seen"]))
        dups += [(r["key"],) for r in rows[1:]]
    if dups:
        con.executemany("UPDATE jobs SET status='dup' WHERE key=?", dups)
        con.commit()
    return len(dups)


def rescore(cfg: dict, con, log=print) -> int:
    """Re-apply filters, tiers and pay estimates to stored postings (after editing config or rules).
    Never triggers alerts."""
    sets = tier_sets(cfg)
    board_tier = {b["key"]: b["tier"] for b in load_boards()}
    n = 0
    for r in con.execute("SELECT * FROM jobs").fetchall():
        n += _rescore_job(cfg, con, r, board_tier.get(r["board"]) or tier_of(r["company"], sets))
    con.commit()
    d = dedupe(con)
    log(f"[rescore] {n} matching postings" + (f"; {d} duplicates merged" if d else ""))
    return n


def _rescore_job(cfg: dict, con, r, tier: str) -> int:
    pay = (r["pay_lo"], r["pay_hi"]) if r["pay_lo"] else None
    f = evaluate(cfg, tier, r["title"], json.loads(r["locations"] or "[]"), None, company=r["company"], pay=pay,
                 terms=json.loads(r["terms"] or "null"), degrees=json.loads(r["degrees"] or "null"))
    con.execute("UPDATE jobs SET tier=?, matched=?, roles=?, cities=?, remote=?, reasons=?, pay_est=? WHERE key=?",
                (tier, f["matched"], f["roles"], f["cities"], f["remote"], f["reasons"], f["pay_est"], r["key"]))
    return f["matched"]


def _board_rows(con) -> dict:
    return {r["key"]: r for r in con.execute("SELECT * FROM boards")}


def _broken(row) -> bool:
    return row is not None and row["fails"] >= BROKEN_FAILS


def _interval(cfg: dict, b: dict, row) -> int:
    p = cfg["poll"]
    every = (p["fast_minutes"] if b["tier"] in TIERED else p["slow_minutes"]) * 60
    return max(every, BROKEN_RETRY) if _broken(row) else every


def _is_due(cfg: dict, b: dict, row, t: float, force: bool, now: bool) -> bool:
    if force or row is None or (now and not _broken(row)):
        return True
    return t - (row["last_poll"] or 0) >= _interval(cfg, b, row)


def _quick_listing(b: dict, row, t: float, force: bool, lane: str) -> bool:
    """Incremental boards list only recent postings unless a complete listing is due. The priority lane always
    lists quickly so its pass stays ~10 s; the slow lane does those complete listings (_priority_full_listings)."""
    return bool(b["ats"] in INCREMENTAL_ATS and not force and row is not None and row["last_full"]
                and (lane == "fast" or t - row["last_full"] < FULL_EVERY))


def _full_listing_due(row, t: float) -> bool:
    """A broken board still waits out BROKEN_RETRY: it never records a complete listing, so would otherwise be
    due every pass."""
    if row is None:
        return True
    if _broken(row) and t - (row["last_poll"] or 0) < BROKEN_RETRY:
        return False
    return t - (row["last_full"] or 0) >= FULL_EVERY


def _due(cfg, con, boards, force: bool, now: bool = False, lane: str = "all") -> list[dict]:
    """Boards to poll this pass. force = everything, complete listings (the "full check" button);
    now = everything except broken boards, quick listings ("Check now")."""
    rows, t = _board_rows(con), db.now()
    return [{**b, "_incremental": _quick_listing(b, rows.get(b["key"]), t, force, lane)}
            for b in boards if _is_due(cfg, b, rows.get(b["key"]), t, force, now)]


def _priority_full_listings(con, boards: list[dict]) -> list[dict]:
    """Priority incremental boards due a complete listing, polled by the slow lane off the priority lane's clock."""
    rows, t = _board_rows(con), db.now()
    return [{**b, "_incremental": False} for b in boards
            if LANES["fast"](b) and b["ats"] in INCREMENTAL_ATS and _full_listing_due(rows.get(b["key"]), t)]


def _matches_only(b: dict, only: str | None) -> bool:
    return not only or only.lower() in (b["company"] + " " + b["key"]).lower()


def _boards_due(cfg: dict, con, force: bool, now: bool, only: str | None, lane: str) -> list[dict]:
    boards = [b for b in load_boards() if _matches_only(b, only)]
    due = _due(cfg, con, [b for b in boards if LANES[lane](b)], force, now, lane)
    if lane == "slow":
        due += _priority_full_listings(con, boards)
    return [b for b in due if b["key"] not in _inflight]  # still downloading from an earlier pass


def _fetch(b: dict):
    t = time.time()
    try:
        return b, FETCH[b["ats"]](b), None
    except Exception as e:  # noqa: BLE001
        return b, None, f"{type(e).__name__}: {e}"[:300]
    finally:
        b["_secs"] = time.time() - t


def _detail(b: dict, job: dict) -> str:
    try:
        return ats.DETAIL[b["ats"]](b, job)
    except Exception:  # noqa: BLE001
        return ""


def _start_fetches(cfg: dict, boards: list[dict]) -> None:
    global _pool
    if _pool is None:
        _pool = cf.ThreadPoolExecutor(cfg["poll"]["workers"])
    for b in boards:
        _inflight[b["key"]] = _pool.submit(_fetch, b)


def _finished_fetches(force: bool, progress) -> list[tuple]:
    """(board, postings, error) for each fetch that finishes in time. The full check (force) waits for all of
    them; regular passes stop waiting after PASS_WAIT and leave the rest in _inflight."""
    waiting = list(_inflight.values())
    finished = []
    try:
        for fut in cf.as_completed(waiting, timeout=None if force else PASS_WAIT):
            finished.append(fut.result())
            if progress:
                progress(len(finished), len(waiting))
    except cf.TimeoutError:
        pass
    for b, _, _ in finished:
        _inflight.pop(b["key"], None)
    return finished


# Each board's writes are committed right away: never hold the write lock while waiting on the network.

def _save_failed_board(con, b: dict, error: str) -> None:
    con.execute("INSERT INTO boards(key, last_poll, last_error, fails) VALUES(?,?,?,1) ON CONFLICT(key) DO "
                "UPDATE SET last_poll=excluded.last_poll, last_error=excluded.last_error, fails=fails+1",
                (b["key"], db.now(), error))
    con.commit()


def _postings_by_key(b: dict, posts: list[dict]) -> dict[str, dict]:
    """Job key -> posting; the first copy wins when a board lists a job twice."""
    out = {}
    for p in posts:
        out.setdefault(f"{b['key']}:{p['job_id']}", p)
    return out


def _update_closed(con, known: dict, listed: dict, t: float, incremental: bool) -> None:
    reopened = [(k,) for k in listed if k in known and known[k] is not None]
    # An incremental listing only shows recent postings, so absence there doesn't mean closed.
    closed = [] if incremental else [(t, k) for k, c in known.items() if c is None and k not in listed]
    if reopened:
        con.executemany("UPDATE jobs SET closed_at=NULL WHERE key=?", reopened)
    if closed:
        con.executemany("UPDATE jobs SET closed_at=? WHERE key=?", closed)


def _mark_board_ok(con, b: dict, t: float, n_jobs: int, full: bool) -> None:
    """Reset the failure count; n_jobs and last_full only change after a complete listing."""
    con.execute("INSERT INTO boards(key, last_poll, last_ok, last_error, n_jobs, fails, last_full) "
                "VALUES(?,?,?,NULL,?,0,?) ON CONFLICT(key) DO UPDATE SET last_poll=excluded.last_poll, "
                "last_ok=excluded.last_ok, last_error=NULL, fails=0, "
                "n_jobs=COALESCE(excluded.n_jobs, boards.n_jobs), last_full=COALESCE(excluded.last_full, boards.last_full)",
                (b["key"], t, t, n_jobs if full else None, t if full else None))


def _save_board(con, b: dict, posts: list[dict], baseline: bool) -> list[tuple]:
    """Reopen/close known postings and mark the board healthy. Returns (board, posting, key, baseline) for each
    posting we haven't stored yet."""
    t = db.now()
    incremental = bool(b.get("_incremental"))
    # One indexed query per board (not one per posting): which postings we know, and which are closed.
    known = {r[0]: r[1] for r in con.execute("SELECT key, closed_at FROM jobs WHERE board=? AND source='board'",
                                             (b["key"],))}
    listed = _postings_by_key(b, posts)
    _update_closed(con, known, listed, t, incremental)
    _mark_board_ok(con, b, t, len(posts), full=not incremental)
    con.commit()
    return [(b, p, key, baseline) for key, p in listed.items() if key not in known]


def _needs_description(cfg: dict, b: dict, p: dict) -> bool:
    """Only plausible postings (title/location pre-filter) are worth a description download (pay, extra locations)."""
    return (p.get("description") is None and b["ats"] in ats.DETAIL
            and not set(judge(p["title"], p["locations"], cfg).reasons) - {"location"})


def _fill_descriptions(cfg: dict, fresh: list[tuple]) -> int:
    need = [(b, p) for b, p, *_ in fresh if _needs_description(cfg, b, p)]
    if need:
        boards, posts = zip(*need)
        with cf.ThreadPoolExecutor(cfg["poll"]["workers"]) as ex:
            for p, desc in zip(posts, ex.map(_detail, boards, posts)):
                p["description"] = desc
    return len(need)


def _list_twin(con, url_key: str, title_key: str):
    """A community-list row for the same job, stored before the board showed it."""
    return con.execute("SELECT key, first_seen, list_source FROM jobs WHERE source='list' AND status!='dup' "
                       "AND (url_key=? OR title_key=?) LIMIT 1", (url_key, title_key)).fetchone()


def _record_posting(cfg: dict, con, b: dict, p: dict, key: str, baseline: bool, t: float) -> dict | None:
    """Store a new board posting; returns its alert when it's a match nobody has been told about."""
    fields = evaluate(cfg, b["tier"], p["title"], p["locations"], p.get("description"), company=b["company"])
    uk, tk = _match_keys(b["company"], p["title"], p.get("url"))
    twin = _list_twin(con, uk, tk)
    notified = 1 if baseline or twin else 0
    inserted = _insert_job(con, {
        "key": key, "source": "board", "board": b["key"], "company": b["company"], "tier": b["tier"],
        "title": p["title"], "locations": json.dumps(p["locations"]), "url": p.get("url"), "url_key": uk,
        "title_key": tk, "posted_at": p.get("posted_at"), "first_seen": t, "last_seen": t, **fields,
        "list_seen_at": twin["first_seen"] if twin else None,
        "list_source": twin["list_source"] if twin else None, "notified": notified}, ignore=True)
    if twin:
        con.execute("UPDATE jobs SET status='dup' WHERE key=?", (twin["key"],))
    if fields["matched"] and not notified and inserted:  # not inserted: another lane may have just added it
        loc = fields["cities"] or ("remote" if fields["remote"] else "")
        return _alert(key, b["company"], p["title"], p.get("url"), loc, fields)
    return None


def _record_postings(cfg: dict, con, fresh: list[tuple]) -> list[dict]:
    t = db.now()
    alerts = [a for item in fresh if (a := _record_posting(cfg, con, *item, t))]
    con.commit()
    return alerts


def _drop_dups(con, alerts: list[dict]) -> list[dict]:
    """Merge cross-board duplicates, and don't alert on a posting that turned out to be one."""
    if not dedupe(con):
        return alerts
    dup = {r[0] for r in con.execute("SELECT key FROM jobs WHERE status='dup' AND source='board'")}
    return [a for a in alerts if a["key"] not in dup]


def poll_boards(cfg: dict, con, *, force: bool = False, now: bool = False, only: str | None = None, lane: str = "all",
                log=print, progress=None) -> list[dict]:
    due = _boards_due(cfg, con, force, now, only, lane)
    t0 = time.time()
    _start_fetches(cfg, due)
    if not _inflight:
        return []
    seen_boards = {r["key"] for r in con.execute("SELECT key FROM boards WHERE last_ok IS NOT NULL")}
    done, fresh, failed = [], [], 0
    for b, posts, error in _finished_fetches(force, progress):
        done.append(b)
        if error:
            failed += 1
            _save_failed_board(con, b, error)
        else:
            fresh += _save_board(con, b, posts, baseline=b["key"] not in seen_boards)
    t_fetch, t1 = time.time() - t0, time.time()
    n_described = _fill_descriptions(cfg, fresh)
    alerts = _record_postings(cfg, con, fresh)
    slowest = max(done, key=lambda b: b.get("_secs", 0), default={"key": "none"})
    log(f"[poll:{lane}] {len(done)} boards ({len(done) - failed} ok, {failed} failed) in {time.time() - t0:.0f}s"
        f"{f', {len(_inflight)} still downloading' if _inflight else ''}; "
        f"{len(fresh)} new postings, {len(alerts)} new matches "
        f"[fetch {t_fetch:.0f}s, slowest {slowest['key'].split('/')[0]} {slowest.get('_secs', 0):.0f}s; "
        f"{n_described} descriptions {time.time() - t1:.0f}s]")
    return _drop_dups(con, alerts)


def _read_lists(log) -> tuple[list[dict], dict]:
    """All community-list rows plus speedyapply's company tiers; a failing list is logged and skipped."""
    rows, speedy_tiers = [], {}
    for name, fn in lists.LISTS.items():
        try:
            rows += fn()
        except Exception as e:  # noqa: BLE001
            log(f"[lists] {name} failed: {e}")
    try:
        sp, speedy_tiers = lists.speedy()
        rows += sp
    except Exception as e:  # noqa: BLE001
        log(f"[lists] speedyapply failed: {e}")
    return rows, speedy_tiers


def _listed_at(r: dict, t: float) -> float:
    return min(t, r["posted_at"]) if r.get("posted_at") else t


def _add_list_job(cfg: dict, con, sets, r: dict, key: str, t: float, first_time: bool) -> dict | None:
    """Store a posting only the lists know about; returns its alert when it's a match (never on the first run)."""
    tier = tier_of(r["company"], sets)
    fields = evaluate(cfg, tier, r["title"], r["locations"], r.get("description"), r.get("terms"),
                      r.get("degrees"), company=r["company"])
    uk, tk = _match_keys(r["company"], r["title"], r.get("url"))
    _insert_job(con, {
        "key": key, "source": "list", "board": r["source"], "company": r["company"], "tier": tier,
        "title": r["title"], "locations": json.dumps(r["locations"]), "url": r.get("url"), "url_key": uk,
        "title_key": tk, "posted_at": r.get("posted_at"), "first_seen": t, "last_seen": t, **fields,
        "list_seen_at": _listed_at(r, t), "list_source": r["source"], "notified": int(first_time),
        "terms": json.dumps(r.get("terms")), "degrees": json.dumps(r.get("degrees"))})
    if fields["matched"] and not first_time:
        return _alert(key, r["company"], r["title"], r.get("url"), fields["cities"], fields)
    return None


def _ingest_list_row(cfg: dict, con, sets, r: dict, t: float, first_time: bool) -> tuple[str, dict | None]:
    """Returns (outcome, alert). outcome: 'linked' (first list sighting of a board posting), 'known' (already
    linked), 'seen' (a list row we already have) or 'added'."""
    uk, tk = _match_keys(r["company"], r["title"], r.get("url"))
    hit = con.execute("SELECT key, list_seen_at FROM jobs WHERE source='board' AND (url_key=? OR title_key=?) "
                      "LIMIT 1", (uk, tk)).fetchone()
    if hit:
        if hit["list_seen_at"] is not None:
            return "known", None
        con.execute("UPDATE jobs SET list_seen_at=?, list_source=? WHERE key=?",
                    (_listed_at(r, t), r["source"], hit["key"]))
        return "linked", None
    key = f"list:{r['source']}:{r['job_id']}"
    if con.execute("SELECT 1 FROM jobs WHERE key=? OR (source='list' AND url_key=? AND url_key!='')",
                   (key, uk)).fetchone():
        con.execute("UPDATE jobs SET last_seen=?, terms=?, degrees=? WHERE key=?",
                    (t, json.dumps(r.get("terms")), json.dumps(r.get("degrees")), key))
        return "seen", None
    return "added", _add_list_job(cfg, con, sets, r, key, t, first_time)


def poll_lists(cfg: dict, con, *, force: bool = False, log=print) -> list[dict]:
    """Ingest the community lists. Postings we already have get list_seen_at (our lead time);
    postings we don't have become source='list' rows (companies we can't poll directly)."""
    last = db.get_meta(con, "lists_polled", 0)
    if not force and db.now() - last < cfg["poll"]["lists_minutes"] * 60:
        return []
    first_time = not last
    rows, speedy_tiers = _read_lists(log)
    sets = tier_sets(cfg, speedy_tiers)
    t = db.now()
    outcomes, alerts = collections.Counter(), []
    for r in rows:
        outcome, alert = _ingest_list_row(cfg, con, sets, r, t, first_time)
        outcomes[outcome] += 1
        if alert:
            alerts.append(alert)
    db.set_meta(con, "lists_polled", t)
    if first_time or db.get_meta(con, "watch_start") is None:
        db.set_meta(con, "watch_start", t)  # lead-time stats only count postings that appear after this
    con.commit()
    log(f"[lists] {len(rows)} list postings: {outcomes['linked']} newly matched to boards we watch, "
        f"{outcomes['added']} added")
    return alerts


def announce(cfg: dict, con, new: list[dict], log=print) -> None:
    if not new:
        return
    notify.send(cfg, new)
    con.executemany("UPDATE jobs SET notified=1 WHERE key=?", [(j["key"],) for j in new])
    con.commit()
    for j in new:
        log(f"NEW {j['company']} | {j['title']} | {j['loc'] or '?'} | {j['pay'] or 'pay not listed'} | {j['url']}")


def run_once(cfg: dict, con, *, force: bool = False, only: str | None = None, log=print) -> int:
    new = poll_boards(cfg, con, force=force, only=only, log=log)
    if not only:
        new += poll_lists(cfg, con, force=force, log=log)
    announce(cfg, con, new, log=log)
    return len(new)
