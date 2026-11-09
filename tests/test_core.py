"""Offline tests: filters, pay parsing, board discovery from URLs, tiering, URL matching, and a poll against a
fake board (no network)."""
import time
import tomllib
from pathlib import Path

import pytest

from jobscraper import companies, db, poller
from jobscraper.filters import judge, parse_hourly, roles_of, season_ok

CFG = tomllib.load(open(Path(__file__).resolve().parents[1] / "config.toml", "rb"))


@pytest.mark.parametrize("title,locs,keep", [
    ("Software Engineer Intern, Summer 2027", ["New York, NY"], True),
    ("Machine Learning Engineer Intern", ["Seattle, WA"], True),
    ("Quantitative Trader Intern", ["Chicago, IL"], True),
    ("Data Science Intern", ["Remote - US"], True),
    ("Software Engineer Intern, Summer 2027", ["Denver, CO"], False),      # city
    ("Software Engineer Intern, Summer 2027", ["London, UK"], False),
    ("Software Engineer Intern, Fall 2026", ["New York, NY"], False),       # season
    ("Software Engineer Intern - Summer 2026", ["New York, NY"], False),
    ("PhD Research Intern, Machine Learning", ["San Francisco, CA"], False),  # degree
    ("Mechanical Engineering Intern", ["Austin, TX"], False),               # role
    ("Marketing Intern", ["Boston, MA"], False),
    ("Senior Software Engineer", ["New York, NY"], False),                  # not an internship
    ("Software Engineering Intern", ["Mountain View, CA"], True),
    ("Software Engineer Intern", ["Bellevue, Washington, USA"], True),
])
def test_judge(title, locs, keep):
    assert judge(title, locs, CFG).keep is keep, judge(title, locs, CFG)


def test_roles_and_season():
    assert set(roles_of("Quantitative Researcher Intern")) == {"quant"}
    assert "ml" in roles_of("AI Research Engineer Intern") and "swe" in roles_of("Backend Software Intern")
    assert roles_of("Hardware Design Intern") == []
    assert season_ok("Software Intern") and season_ok("Summer 2027 SWE Intern")
    assert not season_ok("Winter 2027 Co-op") and season_ok("Summer/Fall 2027 Co-op")
    assert season_ok("x", terms=["Summer 2027"]) and not season_ok("x", terms=["Fall 2026"])
    assert season_ok("x", terms=["N/A"]) and season_ok("x", terms=["Spring/Summer 2027"])
    assert not season_ok("Winter 2027 Co-op", terms=["N/A"])


@pytest.mark.parametrize("text,expected", [
    ("Pay: $45/hr", (45, 45)),
    ("The hourly range is $40 - $55", (40, 55)),
    ("Salary: $120,000 - $150,000 annually", (57.69, 72.12)),
    ("$8,000/month", (46.15, 46.15)),
    ("We raised $500 million", None),
    ("An estimated $124 trillion of assets... US Zone 1: $24.65 - $29 per hour", (24.65, 29)),
    ("no pay listed", None),
])
def test_parse_hourly(text, expected):
    got = parse_hourly(text)
    assert (got is None) if expected is None else got == pytest.approx(expected, abs=0.01)


def test_board_from_url_and_keys():
    b = companies.board_from_url("https://job-boards.greenhouse.io/doordashusa/jobs/8263774")
    assert b == {"ats": "greenhouse", "slug": "doordashusa"}
    b = companies.board_from_url("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA/x_JR1")
    assert b["host"] == "nvidia.wd5.myworkdayjobs.com" and b["site"] == "NVIDIAExternalCareerSite"
    b = companies.board_from_url("https://wd1.myworkdaysite.com/recruiting/snapchat/snap/job/x")
    assert b["tenant"] == "snapchat" and b["url_prefix"] == "recruiting/snapchat/"
    assert companies.board_from_url("https://jobs.lever.co/palantir/abc")["slug"] == "palantir"
    assert companies.board_from_url("https://jobs.ashbyhq.com/openai/123")["slug"] == "openai"
    assert companies.board_from_url("https://www.google.com/about/careers") is None
    assert db.url_key("https://stripe.com/jobs/search?gh_jid=123") == db.url_key("https://boards.greenhouse.io/stripe/jobs/123")
    assert db.url_key("https://jobs.lever.co/x/abc/apply") == db.url_key("https://jobs.lever.co/x/abc")


def test_tiers():
    sets = companies.tier_sets(CFG)
    assert companies.tier_of("Citadel Securities", sets) == "quant"
    assert companies.tier_of("D. E. Shaw & Co.", sets) == "quant"
    assert companies.tier_of("Amazon Web Services", sets) == "faang"
    assert companies.tier_of("Acme Plumbing", sets) == "other"


def test_poll_baseline_then_new(tmp_path, monkeypatch):
    """First poll of a board is silent; a posting that appears later is a new match; list linking records lead."""
    board = {"ats": "fake", "slug": "acme", "company": "Stripe", "tier": "faang", "key": "fake:acme"}
    posts = [{"job_id": "1", "title": "Software Engineer Intern", "locations": ["New York, NY"],
              "url": "https://jobs.lever.co/acme/1", "posted_at": None, "description": "Pay: $50/hr"}]
    monkeypatch.setitem(poller.FETCH, "fake", lambda b: list(posts))
    monkeypatch.setattr(poller, "load_boards", lambda: [board])
    monkeypatch.setattr(poller.lists, "LISTS", {})
    monkeypatch.setattr(poller.lists, "speedy", lambda: ([], {}))
    monkeypatch.setattr(poller.notify, "send", lambda cfg, jobs: None)
    con = db.connect(tmp_path / "t.db")
    assert poller.run_once(CFG, con, force=True, log=lambda *a: None) == 0          # baseline
    posts.append({"job_id": "2", "title": "Quant Developer Intern", "locations": ["Chicago, IL"],
                  "url": "https://jobs.lever.co/acme/2", "posted_at": None, "description": "$60 per hour"})
    assert poller.run_once(CFG, con, force=True, log=lambda *a: None) == 1
    row = con.execute("SELECT * FROM jobs WHERE key='fake:acme:2'").fetchone()
    assert row["matched"] == 1 and row["pay_lo"] == 60 and row["cities"] == "chicago"
    posts.pop(0)
    poller.run_once(CFG, con, force=True, log=lambda *a: None)
    assert con.execute("SELECT closed_at FROM jobs WHERE key='fake:acme:1'").fetchone()[0] is not None


def test_pay_estimate_lets_known_payers_through():
    f = poller.evaluate(CFG, "other", "Software Engineer Intern", ["New York, NY"], "no pay here", company="Capital One")
    assert f["matched"] == 1 and f["pay_est"] == 50 and f["pay_lo"] is None
    f = poller.evaluate(CFG, "other", "Software Engineer Intern", ["New York, NY"], "", company="Acme Plumbing")
    assert f["matched"] == 0 and "company" in f["reasons"]
    f = poller.evaluate(CFG, "other", "Software Engineer Intern", ["New York, NY"], "$30/hr", company="Capital One")
    assert f["matched"] == 0 and f["pay_est"] is None  # posted pay beats the estimate


def test_tier_prefix_needs_known_suffix():
    sets = companies.tier_sets(CFG)
    assert companies.tier_of("Bloomberg Philanthropies", sets) == "other"
    assert companies.tier_of("Intuitive Surgical", sets) == "other"
    assert companies.tier_of("Sierra Space", sets) == "other"
    assert companies.board_from_url("https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/job/1") \
        == {"ats": "oracle", "host": "eeho.fa.us2.oraclecloud.com", "site": "CX_45001"}


def test_operations_titles_and_range_midpoint():
    assert roles_of("Search Safety Operations Intern (TikTok-Platform Responsibility)") == []
    assert roles_of("Process Engineering Intern") == []
    f = poller.evaluate(CFG, "other", "Software Engineer Intern", ["New York, NY"], "$22 - $41 per hour", company="Acme")
    assert f["matched"] == 0 and "pay" in f["reasons"]


def test_incremental_listing_does_not_close_postings(tmp_path, monkeypatch):
    board = {"ats": "fakewd", "slug": "acme", "company": "Stripe", "tier": "faang", "key": "fakewd:acme"}
    posts = [{"job_id": str(i), "title": "Software Engineer Intern", "locations": ["New York, NY"],
              "url": f"https://x/{i}", "posted_at": None, "description": ""} for i in range(3)]
    monkeypatch.setitem(poller.FETCH, "fakewd", lambda b: list(posts) if not b.get("_incremental") else posts[:1])
    monkeypatch.setattr(poller, "INCREMENTAL_ATS", {"fakewd"})
    monkeypatch.setattr(poller, "load_boards", lambda: [board])
    con = db.connect(tmp_path / "t.db")
    poller.poll_boards(CFG, con, force=True, log=lambda *a: None)            # full listing: 3 open
    con.execute("UPDATE boards SET last_poll = 0")                         # make it due again
    poller.poll_boards(CFG, con, log=lambda *a: None)                         # incremental: sees only 1
    assert con.execute("SELECT COUNT(*) FROM jobs WHERE closed_at IS NULL").fetchone()[0] == 3


def test_intern_manager_titles_are_not_internships():
    from jobscraper.filters import roles_of
    assert roles_of("Mgr Electronics Engineering 1 - Ground Systems Intern Development Manager") == []
    assert roles_of("Software Engineer Intern") == ["swe"]


def test_same_job_on_two_boards_shows_once(tmp_path):
    from jobscraper import db, poller
    con = db.connect(tmp_path / "j.db")
    rows = [("a:1", "wd:ivz", "new", 2.0), ("b:1", "wd:ivzearly", "applied", 3.0), ("c:1", "wd:ivz", "new", 1.0)]
    for key, board, status, seen in rows:
        con.execute("INSERT INTO jobs(key, source, board, title_key, locations, status, first_seen, matched) "
                    "VALUES(?, 'board', ?, ?, '[\"Boston\"]', ?, ?, 1)",
                    (key, board, "invesco|quantintern" if key != "c:1" else "invesco|other", status, seen))
    assert poller.dedupe(con) == 1
    st = dict(con.execute("SELECT key, status FROM jobs"))
    assert st == {"a:1": "dup", "b:1": "applied", "c:1": "new"}  # keeps the one you applied to


def test_check_now_polls_every_board_quickly_and_full_listings_stay_off_the_fast_lane(tmp_path):
    from jobscraper import db, poller
    con = db.connect(tmp_path / "j.db")
    t = db.now()
    con.execute("INSERT INTO boards(key, last_poll, last_ok, fails, last_full) VALUES('workday:x', ?, ?, 0, ?)",
                (t, t, t - 7 * 3600))  # polled just now; complete listing is stale
    cfg = {"poll": {"fast_minutes": 5, "slow_minutes": 30}}
    b = [{"key": "workday:x", "ats": "workday", "tier": "faang"}]
    assert poller._due(cfg, con, b, force=False, lane="fast") == []           # not due on schedule
    (d,) = poller._due(cfg, con, b, force=False, now=True, lane="fast")      # Check now
    assert d["_incremental"] is True                                          # quick listing on the fast lane
    (d,) = poller._due(cfg, con, b, force=True, lane="fast")                 # full check button
    assert d["_incremental"] is False


def test_a_slow_board_does_not_hold_up_the_pass(tmp_path, monkeypatch):
    import threading
    release = threading.Event()
    quick = {"ats": "fake", "slug": "q", "company": "Stripe", "tier": "faang", "key": "fake:q"}
    slow = {"ats": "fakeslow", "slug": "s", "company": "Stripe", "tier": "faang", "key": "fakeslow:s"}
    post = lambda i: [{"job_id": i, "title": "Software Engineer Intern", "locations": ["New York, NY"],  # noqa: E731
                       "url": f"https://x/{i}", "posted_at": None, "description": ""}]
    monkeypatch.setitem(poller.FETCH, "fake", lambda b: post("1"))
    monkeypatch.setitem(poller.FETCH, "fakeslow", lambda b: release.wait(5) and post("2"))
    monkeypatch.setattr(poller, "load_boards", lambda: [quick, slow])
    monkeypatch.setattr(poller, "PASS_WAIT", 0.3)
    con = db.connect(tmp_path / "t.db")
    t = time.time()
    poller.poll_boards(CFG, con, now=True, log=lambda *a: None)
    assert time.time() - t < 2                                                 # didn't wait for the slow board
    assert [r[0] for r in con.execute("SELECT key FROM jobs")] == ["fake:q:1"]
    assert "fakeslow:s" in poller._inflight
    release.set()
    poller._inflight["fakeslow:s"].result(timeout=5)
    poller.poll_boards(CFG, con, log=lambda *a: None)                          # a later pass saves it
    assert con.execute("SELECT COUNT(*) FROM jobs WHERE key='fakeslow:s:2'").fetchone()[0] == 1
    assert not poller._inflight


@pytest.mark.parametrize("title,locs,keep,roles", [
    ("Finance Intern [2027 Internship Program]", ["Jersey City, NJ, United States"], True, ["finance"]),
    ("2027 Summer Intern- Marketing", ["New York, NY"], True, ["marketing"]),
    ("Human Resources Intern", ["Boston, MA"], True, ["hr"]),
    ("Accounting Intern", ["Iselin, NJ"], True, ["accounting"]),
    ("Procurement Intern", ["Parsippany, New Jersey"], True, ["business"]),
    ("Marketing Intern", ["Chicago, IL"], False, ["marketing"]),                  # area
    ("Software Engineer Intern", ["New York, NY"], False, []),                    # tech roles stay on the Jobs tab
    ("Senior Director, Internal Audit", ["New York, NY"], False, []),             # not an internship
    ("Macy's 2027 Summer Internship - Marketing MBA", ["New York, NY"], False, []),
    ("Tax/Audit Internship 2028", ["Little Falls, NJ"], False, []),               # season
    ("SEO Marketing Intern/Volunteer", ["New York, NY"], False, []),
])
def test_judge_business(title, locs, keep, roles):
    from jobscraper.filters import judge_business
    v = judge_business(title, locs)
    assert v.keep is keep, v
    if keep:
        assert v.roles == roles


def test_business_front_office_is_flagged_hard():
    from jobscraper.filters import judge_business
    assert judge_business("Investment Banking Summer Analyst Intern", ["New York, NY"]).hard
    assert not judge_business("Finance Intern", ["New York, NY"]).hard


def test_slow_lane_full_listing_respects_broken_board_backoff(tmp_path, monkeypatch):
    """A priority Workday board that always fails never records a complete listing; the slow lane must still
    wait the broken-board retry interval instead of re-polling it every pass."""
    board = {"ats": "fakewd", "slug": "intel", "company": "Stripe", "tier": "faang", "key": "fakewd:intel"}
    calls = []
    monkeypatch.setitem(poller.FETCH, "fakewd", lambda b: calls.append(b["key"]) or [])
    monkeypatch.setattr(poller, "INCREMENTAL_ATS", {"fakewd"})
    monkeypatch.setattr(poller, "load_boards", lambda: [board])
    con = db.connect(tmp_path / "t.db")
    con.execute("INSERT INTO boards(key, last_poll, fails) VALUES('fakewd:intel', ?, 23889)", (db.now() - 60,))
    poller.poll_boards(CFG, con, lane="slow", log=lambda *a: None)
    assert calls == []                                                         # polled a minute ago: rest
    con.execute("UPDATE boards SET last_poll = ?", (db.now() - 7 * 3600,))
    poller.poll_boards(CFG, con, lane="slow", log=lambda *a: None)
    assert calls == ["fakewd:intel"]                                           # retry window passed


def test_failed_pass_rolls_back(tmp_path, monkeypatch):
    """A pass that dies mid-transaction must not leave the lane's connection holding the database."""
    from jobscraper import worker
    con = db.connect(tmp_path / "t.db")

    def dies_mid_write(lane, cfg, c, st, force, now):
        c.execute("INSERT INTO meta(k, v) VALUES('x', '1')")
        raise RuntimeError("boom")

    monkeypatch.setattr(worker, "_one_pass", dies_mid_write)
    monkeypatch.setattr(worker, "log_line", lambda *a: None)
    monkeypatch.setattr(worker, "load_config", lambda: CFG)
    st = type("St", (), {"set": lambda self, **kw: None})()
    worker._safe_pass("fast", con, st, False, False)

    assert not con.in_transaction
    other = db.connect(tmp_path / "t.db")
    other.execute("PRAGMA busy_timeout=100")
    other.execute("INSERT INTO meta(k, v) VALUES('y', '2')")  # would raise "database is locked" without the rollback
    other.commit()
