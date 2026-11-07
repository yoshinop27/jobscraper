"""Web app: http://localhost:8787.

The server only reads the database and manages background processes; it never searches itself, so it stays
responsive. Searching runs in three worker processes (see worker.py): fast (priority companies), slow (all other
boards) and lists (Simplify / GitHub). Start/stop/check-now/refresh/settings all go through this API.
"""
from __future__ import annotations

import collections
import functools
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jobscraper import companies, config, db, tailor, watchlist, worker
from jobscraper.filters import judge_business

PAGE = Path(__file__).with_name("dashboard.html")
ROOT = Path(__file__).resolve().parent.parent


def read_db() -> sqlite3.Connection:
    """Read-only connection: no schema work, never takes the write lock."""
    con = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def query(read_model, *args):
    """Run a read model on a fresh read-only connection (a read-only connect can't create a missing DB)."""
    if not db.DB_PATH.exists():
        db.connect().close()
    with closing(read_db()) as con:
        return read_model(con, *args)


# --------------------------------------------------------------------------- process control
class Controller:
    """Starts/stops the worker processes and reports what they're doing."""

    STATUS_NAMES = (*worker.LANES, "once-all", "task-discover", "task-rescore")

    def __init__(self):
        self.children: list[subprocess.Popen] = []

    def _spawn(self, *args: str, name: str) -> None:
        with open(worker.DATA / f"worker-{name}.out", "a") as out:
            self.children.append(subprocess.Popen([sys.executable, "-m", "jobscraper", "worker", *args], cwd=ROOT,
                                                  stdout=out, stderr=subprocess.STDOUT, start_new_session=True))

    def _reap(self) -> None:
        self.children = [p for p in self.children if p.poll() is None]

    def lane_pid(self, lane: str) -> int | None:
        st = worker.read_status(lane) or {}
        pid = st.get("pid")
        return pid if st.get("state") != "stopped" and worker.alive(pid) else None

    @property
    def running(self) -> bool:
        return any(self.lane_pid(l) for l in worker.LANES)

    def start(self) -> None:
        config.set_flag("running", True)
        for lane in worker.LANES:
            if not self.lane_pid(lane):
                self._spawn("--lane", lane, name=lane)
        worker.log_line("app", "searching started")

    def stop(self) -> None:
        config.set_flag("running", False)
        self._signal(worker.LANES, signal.SIGTERM)
        worker.log_line("app", "searching stopped")

    def check_now(self, force: bool = False) -> None:
        if force:
            self._spawn("--lane", "all", "--once", "--force", name="full")
            worker.log_line("app", "full check of every board started (~5 min)")
            return
        # SIGUSR1 wakes a sleeping lane; with neither lane running, do a one-off pass instead.
        if not self._signal(("fast", "lists"), signal.SIGUSR1):
            self._spawn("--lane", "all", "--once", name="once")
        worker.log_line("app", "check requested")

    def _signal(self, lanes, sig: int) -> bool:
        """Send `sig` to every live lane in `lanes`; True if any received it."""
        pids = [pid for pid in map(self.lane_pid, lanes) if pid]
        for pid in pids:
            os.kill(pid, sig)
        return bool(pids)

    def task(self, name: str) -> None:
        st = worker.read_status(f"task-{name}") or {}
        if st.get("state") == "running" and worker.alive(st.get("pid")):
            return
        self._spawn("--task", name, name=name)

    def state(self) -> dict:
        self._reap()
        lanes = self._statuses()
        busy = [l for l in lanes if l["alive"] and l.get("state") not in ("idle", "starting")]
        return {"running": self.running, "lanes": lanes, "busy": busy, "log": recent_log()}

    def _statuses(self) -> list[dict]:
        """Every lane, plus one-off runs and tasks while they're alive."""
        out = []
        for name in self.STATUS_NAMES:
            st = worker.read_status(name)
            if not st:
                continue
            st["alive"] = worker.alive(st.get("pid")) and st.get("state") not in ("stopped", "done")
            if name in worker.LANES or st["alive"]:
                out.append(st)
        return out


def recent_log(n: int = 200) -> list[str]:
    try:
        return worker.LOG.read_text().splitlines()[-n:]
    except FileNotFoundError:
        return []


# --------------------------------------------------------------------------- read models
OPEN_MATCH = "matched=1 AND closed_at IS NULL AND status!='dup'"
INTERN_TITLE = " OR ".join(f"title LIKE '%{t}%'" for t in
                           ("intern", "co-op", "co op", "summer analyst", "summer associate", "summer student"))


def jobs_json(con, show_all: bool = False) -> list[dict]:
    where = "status != 'dup'" + ("" if show_all else " AND matched=1")
    cols = ("key, source, company, tier, title, locations, url, posted_at, first_seen, closed_at, roles, cities, "
            "remote, pay_lo, pay_hi, pay_est, list_seen_at, list_source, status")
    rows = con.execute(f"SELECT {cols} FROM jobs WHERE {where} ORDER BY COALESCE(posted_at, 0) DESC, first_seen DESC "
                       "LIMIT 5000").fetchall()
    return [{**r, "locations": json.loads(r["locations"] or "[]")} for r in map(dict, rows)]


def biz_jobs_json(con) -> list[dict]:
    """The Business tab: HR/marketing/accounting/finance/ops internships in NYC, NJ and Boston, judged at read time
    over every open posting (the main filters drop these titles, so they aren't marked matched)."""
    season = config.load_config()["search"]["season"]
    cols = ("key, source, company, tier, title, locations, url, posted_at, first_seen, pay_lo, pay_hi, list_source, "
            "status, terms, degrees")
    rows = con.execute(f"SELECT {cols} FROM jobs WHERE closed_at IS NULL AND status != 'dup' AND ({INTERN_TITLE}) "
                       "ORDER BY COALESCE(posted_at, 0) DESC, first_seen DESC").fetchall()
    out, seen = [], set()
    for r in rows:
        job = _business_job(r, season)
        dup = (companies.norm(r["company"]), r["title"].strip().lower(), r["locations"])  # same job on two boards
        if job and dup not in seen:
            seen.add(dup)
            out.append(job)
    return out


def _business_job(r: sqlite3.Row, season) -> dict | None:
    locs = json.loads(r["locations"] or "[]")
    v = judge_business(r["title"], locs, season=season, terms=json.loads(r["terms"] or "null"),
                       degrees=json.loads(r["degrees"] or "null"))
    if not v.keep:
        return None
    d = {k: r[k] for k in r.keys() if k not in ("terms", "degrees")}
    d.update(locations=locs, roles=",".join(v.roles), cities=",".join(v.cities), remote=int(v.remote),
             competitive=int(v.hard or r["tier"] in ("faang", "quant")))
    return d


def _scalar(con, sql: str, *args):
    return con.execute(sql, args).fetchone()[0]


def _list_lead_times(con) -> list[float]:
    """Seconds by which direct polling beat the lists, for jobs first seen since watching started."""
    try:
        start = json.loads(_scalar(con, "SELECT v FROM meta WHERE k='watch_start'") or "1e12")
    except TypeError:  # no watch_start row yet
        start = 1e12
    return [r[0] for r in con.execute(
        "SELECT list_seen_at - first_seen FROM jobs WHERE source='board' AND list_seen_at IS NOT NULL "
        "AND list_seen_at > first_seen AND first_seen > ?", (start,))]


def stats(con) -> dict:
    q = functools.partial(_scalar, con)
    leads = _list_lead_times(con)
    t = time.time()
    return {"boards": q("SELECT COUNT(*) FROM boards"), "boards_ok": q("SELECT COUNT(*) FROM boards WHERE fails=0"),
            "matched_open": q(f"SELECT COUNT(*) FROM jobs WHERE {OPEN_MATCH}"),
            "new_today": q(f"SELECT COUNT(*) FROM jobs WHERE {OPEN_MATCH} AND posted_at > ?", t - 86400),
            "new_week": q(f"SELECT COUNT(*) FROM jobs WHERE {OPEN_MATCH} AND posted_at > ?", t - 7 * 86400),
            "applied": q("SELECT COUNT(*) FROM jobs WHERE status='applied'"),
            "with_pay": q(f"SELECT COUNT(*) FROM jobs WHERE {OPEN_MATCH} AND pay_lo IS NOT NULL"),
            "last_poll": q("SELECT MAX(last_poll) FROM boards"), "beat_lists": len(leads),
            "median_lead_hours": round(sorted(leads)[len(leads) // 2] / 3600, 1) if leads else None}


def companies_json(con) -> list[dict]:
    """Every priority company and how it's covered (boards polled directly, or via the lists only)."""
    health = {r["key"]: dict(r) for r in con.execute("SELECT * FROM boards")}
    open_counts = collections.Counter(r[0] for r in con.execute(f"SELECT company FROM jobs WHERE {OPEN_MATCH}"))
    boards = companies.load_boards()
    info = functools.partial(_board_info, health=health)
    curated = _curated_companies(boards, info, open_counts)
    seen = {companies.norm(c["company"]) for c in curated}
    return curated + _uncurated_tiered_companies(boards, info, open_counts, seen)


def _board_info(b: dict, health: dict) -> dict:
    h = health.get(b["key"], {})
    return {"key": b["key"], "ats": b["ats"], **{x: h.get(x) for x in ("last_ok", "fails", "n_jobs", "last_error")}}


def _curated_companies(boards: list[dict], info, open_counts) -> list[dict]:
    by_name = collections.defaultdict(list)
    for b in boards:
        by_name[companies.norm(b["company"])].append(b)
    out, seen = [], set()
    for tier, groups in watchlist.parse().items():
        for names in groups:  # display name first, then aliases
            k = companies.norm(names[0])
            if k in seen:
                continue
            seen.add(k)
            bs = [b for n in names for b in by_name.get(companies.norm(n), [])]
            out.append({"company": names[0], "tier": tier, "boards": [info(b) for b in bs],
                        "open_matches": sum(open_counts[n] for n in {b["company"] for b in bs} | {names[0]})})
    return out


def _uncurated_tiered_companies(boards: list[dict], info, open_counts, seen: set) -> list[dict]:
    """Boards tiered via speedyapply/config but not on the curated list."""
    out = []
    for b in boards:
        k = companies.norm(b["company"])
        if b["tier"] != "other" and k not in seen:
            seen.add(k)
            out.append({"company": b["company"], "tier": b["tier"], "open_matches": open_counts[b["company"]],
                        "boards": [info(b)]})
    return out


def tailored_file(path: str) -> Path | None:
    """/tailored/<id>/resume.pdf|resume.tex -> that file, if the path is well-formed and the file exists."""
    parts = path.split("?")[0].split("/")
    if len(parts) == 4 and tailor.ID_RE.match(parts[2]) and parts[3] in ("resume.pdf", "resume.tex"):
        f = tailor.OUT / parts[2] / parts[3]
        if f.exists():
            return f
    return None


def job_by_key(key: str) -> dict | None:
    with closing(read_db()) as con:
        r = con.execute("SELECT key, board, company, title, url, locations FROM jobs WHERE key=?", (key,)).fetchone()
    return {**dict(r), "locations": json.loads(r["locations"] or "[]")} if r else None


# --------------------------------------------------------------------------- HTTP
CONTROL_ACTIONS = {
    "/api/start": lambda ctl, body: ctl.start(),
    "/api/stop": lambda ctl, body: ctl.stop(),
    "/api/poll": lambda ctl, body: ctl.check_now(force=bool(body.get("force"))),
    "/api/discover": lambda ctl, body: ctl.task("discover"),
    "/api/rescore": lambda ctl, body: ctl.task("rescore"),
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    ctl: Controller  # bound per server by make_handler

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _ok(self) -> None:
        self._send(200, {"ok": True})

    def _error(self, code: int, msg: str) -> None:
        self._send(code, {"error": msg})

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        route = self.GET_ROUTES.get(self.path) or next(
            (h for prefix, h in self.GET_PREFIX_ROUTES if self.path.startswith(prefix)), None)
        if not route:
            return self._send(404, {})
        route(self)

    def do_POST(self):
        body = self._body()
        route = self.POST_ROUTES.get(self.path)
        if not route:
            return self._send(404, {})
        route(self, body)

    # ---- GET
    def get_page(self):
        self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")

    def get_tailored_file(self):
        f = tailored_file(self.path)
        if not f:
            return self._send(404, {})
        self._send(200, f.read_bytes(), "application/pdf" if f.suffix == ".pdf" else "text/plain; charset=utf-8")

    def get_resume(self):
        self._send(200, {"tex": tailor.BASE.read_text() if tailor.BASE.exists() else "",
                         "has_key": tailor.has_key(), "history": tailor.history()})

    def get_tailor_meta(self):
        tid = self.path.rsplit("/", 1)[-1]
        m = tailor.meta(tid) if tailor.ID_RE.match(tid) else None
        self._send(200 if m else 404, m or {})

    def get_jobs(self):
        self._send(200, {"jobs": query(jobs_json, "all=1" in self.path)})

    def get_biz(self):
        self._send(200, {"jobs": query(biz_jobs_json)})

    def get_state(self):
        self._send(200, {**self.ctl.state(), "stats": query(stats), "settings": config.load_config()})

    def get_companies(self):
        self._send(200, {"companies": query(companies_json)})

    # ---- POST
    def post_status(self, body):
        if body.get("status") not in ("new", "applied", "hidden"):
            return self._error(400, "bad status")
        with closing(db.connect()) as con:
            con.execute("UPDATE jobs SET status=? WHERE key=?", (body["status"], body.get("key")))
            con.commit()
        self._send(200, {})

    def post_resume(self, body):
        tex = body.get("tex") or ""
        if "\\begin{document}" not in tex:
            return self._error(400, "that doesn't look like a LaTeX document (no \\begin{document})")
        tailor.RESUME_DIR.mkdir(parents=True, exist_ok=True)
        tailor.BASE.write_text(tex)
        self._ok()

    def post_tailor(self, body):
        job = {"company": body.get("company", ""), "title": body.get("title", ""), "url": body.get("url")}
        if body.get("key"):
            job = job_by_key(body["key"])
            if not job:
                return self._error(404, "unknown job")
        if not body.get("description") and not job.get("url"):
            return self._error(400, "paste the job description")
        try:
            tid = tailor.start(job, body.get("description") or "", body.get("notes") or "")
        except ValueError as e:
            return self._error(400, str(e))
        self._send(200, {"id": tid})

    def post_control(self, body):
        CONTROL_ACTIONS[self.path](self.ctl, body)
        self._ok()

    def post_settings(self, body):
        try:
            config.save_settings(body)
        except (ValueError, TypeError) as e:
            return self._error(400, str(e))
        worker.log_line("app", "settings saved; re-applying filters")
        self.ctl.task("rescore")
        self._ok()

    GET_ROUTES = {"/": get_page, "/index.html": get_page, "/api/resume": get_resume, "/api/biz": get_biz,
                  "/api/state": get_state, "/api/companies": get_companies}
    GET_PREFIX_ROUTES = (("/tailored/", get_tailored_file), ("/api/tailor/", get_tailor_meta),
                         ("/api/jobs", get_jobs))  # /api/jobs?all=1
    POST_ROUTES = {"/api/status": post_status, "/api/resume": post_resume, "/api/tailor": post_tailor,
                   "/api/settings": post_settings, **dict.fromkeys(CONTROL_ACTIONS, post_control)}


def make_handler(ctl: Controller) -> type[Handler]:
    return type("H", (Handler,), {"ctl": ctl})


def serve(port: int = 8787, start: bool | None = None, host: str = "127.0.0.1") -> None:
    db.connect().close()  # create/migrate the schema once, up front
    ctl = Controller()
    if start or (start is None and config.get_flag("running", False)):
        ctl.start()
    srv = ThreadingHTTPServer((host, port), make_handler(ctl))
    srv.daemon_threads = True
    print(f"Internship Radar: http://localhost:{port}  (Ctrl+C to quit; searching keeps running until you press "
          "Stop in the app)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
