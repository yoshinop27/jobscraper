"""Job scraper CLI.

  python -m jobscraper discover            build data/boards.json (run once, then weekly)
  python -m jobscraper poll                one poll of everything that's due
  python -m jobscraper poll --only stripe  poll matching boards now (debugging)
  python -m jobscraper serve               web app at http://localhost:8787; runs the background search workers
  python -m jobscraper list [--all]        print current matches
  python -m jobscraper stats               boards, failures, lead time vs. the lists
  python -m jobscraper companies           which companies are polled directly, by tier
  python -m jobscraper rescore             re-apply filters/pay estimates after editing config.toml
  python -m jobscraper export jobs.csv     matched postings to CSV
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
import time

from jobscraper import companies, db, poller
from jobscraper.config import load_config  # config.toml + changes made in the web UI

LIST_ORDER = {"posted": "posted_at IS NULL, posted_at DESC, first_seen DESC", "seen": "first_seen DESC",
              "pay": "COALESCE((pay_lo + pay_hi) / 2, pay_est, 0) DESC",
              "company": "company COLLATE NOCASE, posted_at DESC"}
EXPORT_COLS = ["company", "tier", "title", "cities", "remote", "pay_lo", "pay_hi", "url", "first_seen",
               "list_seen_at", "status"]


def _ago(t) -> str:
    if not t:
        return "-"
    s = time.time() - t
    return f"{s / 60:.0f}m" if s < 3600 else f"{s / 3600:.0f}h" if s < 86400 else f"{s / 86400:.0f}d"


def _stamp(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def cmd_discover(a) -> None:
    companies.discover(load_config())


def cmd_poll(a) -> None:
    cfg = load_config()
    if not companies.BOARDS_FILE.exists():
        companies.discover(cfg)
    poller.run_once(cfg, db.connect(), force=a.force, only=a.only)


def cmd_rescore(a) -> None:
    poller.rescore(load_config(), db.connect())


def cmd_serve(a) -> None:
    from jobscraper.serve import serve
    serve(a.port, a.start, a.host)


def cmd_worker(a) -> None:
    from jobscraper import worker
    if a.task:
        worker.run_task(a.task)
    else:
        worker.run_lane(a.lane, once=a.once, force=a.force)


def cmd_companies(a) -> None:
    by_tier = collections.defaultdict(set)
    for b in companies.load_boards():
        by_tier[b["tier"]].add(f"{b['company']} ({b['ats']})")
    for t in ("faang", "quant", "unicorn", "highpay") + (("other",) if a.all else ()):
        print(f"\n{t} ({len(by_tier[t])}):\n  " + "\n  ".join(sorted(by_tier[t], key=str.lower)))
    if not a.all:
        print(f"\nother: {len(by_tier['other'])} boards (shown only with posted/estimated pay >= min_hourly; --all to list)")


def cmd_list(a) -> None:
    where = "1=1" if a.all else "matched=1 AND closed_at IS NULL AND status NOT IN ('dup','hidden')"
    rows = db.connect().execute(f"SELECT * FROM jobs WHERE {where} ORDER BY {LIST_ORDER[a.sort]} LIMIT ?", (a.n,))
    print(f"{'posted':>6} {'tier':<8} {'company':<22} {'title':<60} {'where':<14} pay")
    for r in rows:
        print(_list_line(r))


def _list_line(r) -> str:
    pay = poller._fmt_pay(r["pay_lo"], r["pay_hi"], r["pay_est"])
    when = time.strftime("%b %d", time.localtime(r["posted_at"])) if r["posted_at"] else "~" + _ago(r["first_seen"])
    where = r["cities"] or ("remote" if r["remote"] else "?")
    return f"{when:>6} {r['tier']:<8} {r['company'][:22]:<22} {r['title'][:60]:<60} {where:<14} {pay:<12} {r['url']}"


def cmd_stats(a) -> None:
    from jobscraper.serve import stats
    con = db.connect()
    print(json.dumps(stats(con), indent=1))
    bad = con.execute("SELECT key, fails, last_error FROM boards WHERE fails>0 ORDER BY fails DESC LIMIT 15").fetchall()
    if bad:
        print("failing boards:")
        for r in bad:
            print(f"  {r['fails']}x {r['key']}: {r['last_error'][:100]}")


def cmd_export(a) -> None:
    rows = db.connect().execute(f"SELECT {', '.join(EXPORT_COLS)} FROM jobs WHERE matched=1 AND status!='dup' "
                                "ORDER BY first_seen DESC")
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(EXPORT_COLS)
        for r in rows:
            w.writerow([*r[:8], _stamp(r[8]), _stamp(r[9]) if r[9] else "", r[10]])
    print(f"wrote {a.out}")


COMMANDS = {"discover": cmd_discover, "poll": cmd_poll, "serve": cmd_serve, "list": cmd_list, "stats": cmd_stats,
            "rescore": cmd_rescore, "worker": cmd_worker, "companies": cmd_companies, "export": cmd_export}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="jobscraper", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("discover")
    p = sub.add_parser("poll")
    p.add_argument("--force", action="store_true", help="poll every board now, ignoring intervals")
    p.add_argument("--only", help="substring of company/board key")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 inside Docker")
    s.add_argument("--start", action="store_true", default=None, help="start searching immediately")
    s.add_argument("--no-start", dest="start", action="store_false", help="open with searching stopped")
    l = sub.add_parser("list")
    l.add_argument("--all", action="store_true", help="include closed and non-matching")
    l.add_argument("-n", type=int, default=60)
    l.add_argument("--sort", choices=list(LIST_ORDER), default="posted")
    sub.add_parser("stats")
    sub.add_parser("rescore")
    w = sub.add_parser("worker", help="background search process (the web app starts these)")
    w.add_argument("--lane", choices=["fast", "slow", "lists", "all"], default="all")
    w.add_argument("--once", action="store_true")
    w.add_argument("--force", action="store_true")
    w.add_argument("--task", choices=["discover", "rescore"])
    c = sub.add_parser("companies")
    c.add_argument("--all", action="store_true", help="also list the 'other' tier")
    e = sub.add_parser("export")
    e.add_argument("out")
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    COMMANDS[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
