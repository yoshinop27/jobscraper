"""Background search processes, one per lane, so searching never slows down the web app and a slow sweep never
delays the fast one:

  fast   FAANG+/quant/unicorn/high-pay boards, every `fast_minutes`
  slow   every other company board, every `slow_minutes`
  lists  Simplify / GitHub lists, every `lists_minutes`

Each lane writes its status to data/status/<lane>.json and appends to data/activity.log.
Signals: SIGUSR1 = check now, SIGTERM = stop.

  python -m jobscraper worker --lane fast            run the fast lane forever
  python -m jobscraper worker --lane all --once      one pass of everything that's due
  python -m jobscraper worker --task discover|rescore
"""
from __future__ import annotations

import functools
import json
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

from jobscraper import companies, db, poller
from jobscraper.config import load_config

DATA = Path(__file__).resolve().parent.parent / "data"
STATUS_DIR = DATA / "status"
LOG = DATA / "activity.log"
LANES = ("fast", "slow", "lists")
_log_lock = threading.Lock()


def log_line(lane: str, msg: str) -> None:
    line = f"{time.strftime('%m-%d %H:%M:%S')} [{lane}] {msg}"
    with _log_lock:
        DATA.mkdir(exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    print(line, flush=True)


def trim_log(max_lines: int = 2000) -> None:
    try:
        lines = LOG.read_text().splitlines()
        if len(lines) > max_lines * 1.5:
            LOG.write_text("\n".join(lines[-max_lines:]) + "\n")
    except FileNotFoundError:
        pass


class Status:
    """Atomic JSON status file for the web app to read."""

    def __init__(self, name: str):
        STATUS_DIR.mkdir(parents=True, exist_ok=True)
        self.path = STATUS_DIR / f"{name}.json"
        self.d = {"lane": name, "pid": os.getpid(), "state": "starting", "progress": None, "last_done": None,
                  "last_duration": None, "next_run": None, "last_result": "", "started": time.time()}
        self._last_write = 0.0
        prev = read_status(name)
        if prev:  # keep history across restarts
            for k in ("last_done", "last_duration", "last_result"):
                self.d[k] = prev.get(k)
        self.write()

    def set(self, throttle: bool = False, **kw) -> None:
        self.d.update(kw)
        if throttle and time.time() - self._last_write < 1:
            return
        self.write()

    def write(self) -> None:
        self._last_write = time.time()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d))
        tmp.replace(self.path)


def read_status(name: str) -> dict | None:
    try:
        return json.loads((STATUS_DIR / f"{name}.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _skip_quiet_passes(say):
    """Drop summaries of passes that found nothing new, so the activity log shows what changed."""
    def log(msg: str) -> None:
        if " 0 new postings" not in msg and "0 newly matched" not in msg:
            say(msg)
    return log


def _one_pass(lane: str, cfg: dict, con, st: Status, force: bool, now: bool = False) -> None:
    say = functools.partial(log_line, lane)
    quiet = _skip_quiet_passes(say)
    t0 = time.time()
    st.set(state="checking", progress=None)
    if not companies.BOARDS_FILE.exists():
        companies.discover(cfg, log=say)
    new = []
    if lane in ("fast", "slow", "all"):
        new += poller.poll_boards(cfg, con, force=force, now=now, lane=lane, log=say if force or now else quiet,
                                  progress=lambda i, n: st.set(throttle=True, progress=[i, n]))
    if lane in ("lists", "all"):
        st.set(state="reading Simplify / GitHub lists")
        new += poller.poll_lists(cfg, con, force=force or now or lane == "all", log=quiet)
    poller.announce(cfg, con, new, log=say)
    st.set(state="idle", progress=None, last_done=time.time(), last_duration=round(time.time() - t0, 1),
           last_result=f"{len(new)} new match{'es' if len(new) != 1 else ''}")


def _safe_pass(lane: str, con, st: Status, force: bool, now: bool) -> None:
    """One pass with freshly loaded config; an error during the pass is logged and the lane keeps running."""
    cfg = load_config()
    try:
        _one_pass(lane, cfg, con, st, force, now)
    except Exception as e:  # noqa: BLE001
        # Without a rollback the connection stays inside the failed transaction, pinned to an old snapshot:
        # every later write then fails with "database is locked" and the WAL file can never be checkpointed.
        con.rollback()
        log_line(lane, f"error: {e}")
        traceback.print_exc()
        st.set(state="idle", last_result=f"error: {e}")


def _handle_signals(st: Status) -> threading.Event:
    """SIGUSR1 sets the returned event ("check now"); SIGTERM records 'stopped' and exits immediately."""
    wake = threading.Event()

    def stop(*_):
        st.set(state="stopped", progress=None)
        os._exit(0)

    signal.signal(signal.SIGUSR1, lambda *_: wake.set())
    signal.signal(signal.SIGTERM, stop)
    return wake


def _wait_for_next_pass(lane: str, st: Status, wake: threading.Event) -> bool:
    """Sleep 30 s (what's due is re-checked every pass) or until "check now"; returns whether it was requested."""
    st.set(next_run=time.time() + 30)
    if not wake.wait(30):
        return False
    wake.clear()
    log_line(lane, "check requested")
    return True


def run_lane(lane: str, once: bool = False, force: bool = False) -> None:
    con = db.connect()
    st = Status(lane if not once else f"once-{lane}")
    wake = _handle_signals(st)
    if not once:
        log_line(lane, "started")
    now = False
    while True:
        _safe_pass(lane, con, st, force, now)
        if once:
            st.set(state="done")
            return
        trim_log()
        now = _wait_for_next_pass(lane, st, wake)  # "check now" re-checks every board in the lane, not just due ones
        force = False


def run_task(task: str) -> None:
    st = Status(f"task-{task}")
    cfg = load_config()
    say = functools.partial(log_line, task)
    st.set(state="running")
    try:
        if task == "discover":
            companies.discover(cfg, log=say)
        elif task == "rescore":
            poller.rescore(cfg, db.connect(), log=say)
        st.set(state="done", last_done=time.time())
    except Exception as e:  # noqa: BLE001
        say(f"error: {e}")
        st.set(state="error", last_result=str(e))
        sys.exit(1)
