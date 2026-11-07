"""Tailor a LaTeX resume to one job posting with Claude, then compile it with pdflatex to prove it builds.

  data/resume/resume.tex          your base resume (upload it in the web app's Resume tab); any .cls/.sty
                                  files it needs can sit next to it
  data/tailored/<id>/             one folder per tailoring: resume.tex, resume.pdf, meta.json

The model may only reorder, rephrase, emphasize and cut what's already in the base resume; anything the
posting asks for that the resume doesn't show is reported as a gap instead of being added.
Needs ANTHROPIC_API_KEY in the environment or in a .env file at the project root.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from jobscraper import companies
from jobscraper.http import get_json, get_text
from jobscraper.sources import ats

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RESUME_DIR = DATA / "resume"
BASE = RESUME_DIR / "resume.tex"
OUT = DATA / "tailored"
MODEL = "claude-opus-5-5"
MAX_FIXES = 2
ID_RE = re.compile(r"^[a-z0-9-]{1,80}$")


def load_env() -> None:
    """Read ANTHROPIC_API_KEY from ROOT/.env if it isn't already in the environment."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    try:
        lines = (ROOT / ".env").read_text().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        k, _, v = line.partition("=")
        if k.strip() == "ANTHROPIC_API_KEY" and v.strip():
            os.environ["ANTHROPIC_API_KEY"] = v.strip().strip("'\"")


def has_key() -> bool:
    load_env()
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


# --------------------------------------------------------------------------- fetch the posting
def html_to_text(s: str) -> str:
    """HTML -> readable text that keeps paragraphs and bullets (the model reads structure)."""
    s = html.unescape(s or "")
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
    s = re.sub(r"(?i)<(br|/p|/div|/h\d|/ul|/ol|/li)[^>]*>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[ \t\r\xa0]+", " ", s)
    s = "\n".join(l.strip() for l in s.splitlines() if l.strip() not in ("-", "|"))  # empty nav bullets
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _job_id(url: str) -> str:
    return url.rstrip("/").split("/")[-1].split("?")[0]


def _greenhouse(board: dict, job: dict, url: str) -> str:
    m = re.search(r"gh_jid=(\d+)", url) or re.search(r"/jobs/(\d+)", url)
    gid = m.group(1) if m else job["key"].split(":")[-1]
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{board['slug']}/jobs/{gid}?pay_transparency=true")
    return html_to_text(d.get("content", ""))


def _lever(board: dict, job: dict, url: str) -> str:
    host = "api.eu.lever.co" if board.get("eu") else "api.lever.co"
    d = get_json(f"https://{host}/v0/postings/{board['slug']}/{_job_id(url)}")
    lists = [f"{x.get('text', '')}\n{html_to_text(x.get('content', ''))}" for x in d.get("lists") or []]
    return "\n\n".join([d.get("descriptionPlain") or "", *lists, d.get("additionalPlain") or ""])


def _ashby(board: dict, job: dict, url: str) -> str:
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board['slug']}")
    job_id = _job_id(url)
    hit = next((j for j in d.get("jobs", []) if j.get("id") == job_id or j.get("jobUrl") == url), None)
    if not hit:
        return ""
    return html_to_text(hit.get("descriptionHtml") or "") or hit.get("descriptionPlain") or ""


def _workday(board: dict, job: dict, url: str) -> str:
    m = re.search(r"(/job/.+)$", url.split("?")[0])
    if not m:
        return ""
    d = get_json(ats._wd_base(board) + m.group(1))
    return html_to_text((d.get("jobPostingInfo") or {}).get("jobDescription", ""))


def _smartrecruiters(board: dict, job: dict, url: str) -> str:
    d = get_json(f"https://api.smartrecruiters.com/v1/companies/{board['slug']}/postings/{_job_id(url)}")
    secs = (d.get("jobAd") or {}).get("sections") or {}
    return "\n\n".join(html_to_text((secs.get(k) or {}).get("text", ""))
                       for k in ("jobDescription", "qualifications", "additionalInformation"))


def _oracle(board: dict, job: dict, url: str) -> str:
    m = re.search(r"/job/(\d+)", url)
    if not m:
        return ""
    d = get_json(f"https://{board['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                 f"?expand=all&onlyData=true&finder=ById;Id=%22{m.group(1)}%22,siteNumber={board['site']}")
    item = (d.get("items") or [{}])[0]
    return "\n\n".join(html_to_text(item.get(k) or "") for k in
                       ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"))


_ATS_FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby, "workday": _workday,
                 "smartrecruiters": _smartrecruiters, "oracle": _oracle}


def _board_for(job: dict, url: str) -> dict | None:
    board = companies.board_from_url(url)
    if not board and job.get("board"):  # custom careers domain (e.g. careers.duolingo.com) for a board we poll
        board = next((b for b in companies.load_boards() if b["key"] == job["board"]), None)
    return board


def _from_api(job: dict, url: str) -> str:
    board = _board_for(job, url)
    if not board:
        return ""
    try:
        fetch = _ATS_FETCHERS.get(board["ats"])
        return fetch(board, job, url) if fetch else ""
    except Exception:  # noqa: BLE001 - fall through to the page
        return ""


def _from_page(url: str) -> str:
    """Fallback: the posting page's JobPosting JSON-LD, else its visible text."""
    page = get_text(url, timeout=20)
    for block in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            if isinstance(d, dict) and d.get("description"):
                return html_to_text(d["description"])
    return html_to_text(page)


def _longer_of(text: str, url: str) -> str:
    """`text` or the posting page's text, whichever is longer."""
    try:
        page = _from_page(url)
    except Exception:  # noqa: BLE001
        return text
    return page if len(page) > len(text) and not page.startswith("{") else text  # "{": a JSON redirect stub


def fetch_description(job: dict) -> str:
    """Full posting text for a job row (any source). Tries the ATS API first, then the posting page."""
    url = job.get("url") or ""
    text = _from_api(job, url)
    if len(text) < 300 and url:
        text = _longer_of(text, url)
    return text.strip()


# --------------------------------------------------------------------------- compile
FONT_FALLBACK = r"\RequirePackage{lmodern}\input{./resume.tex}"


def _log_summary(stdout: str) -> str:
    errs = [l for l in stdout.splitlines() if l.startswith("!") or re.match(r"l\.\d+ ", l)]
    return "\n".join(errs[:12]) or "\n".join(stdout.splitlines()[-25:])


def _pdflatex(workdir: Path, source: str) -> tuple[bool, int | None, str]:
    # Working dir first: the tailored file shares its name with the base resume in RESUME_DIR.
    env = {**os.environ, "TEXINPUTS": f".{os.pathsep}{RESUME_DIR}{os.pathsep}"}
    try:
        r = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-jobname=resume", source],
                           cwd=workdir, env=env, capture_output=True, text=True, errors="replace", timeout=90)
    except subprocess.TimeoutExpired:
        return False, None, "pdflatex timed out"
    m = re.search(r"Output written on \S+ \((\d+) pages?", r.stdout)
    pages = int(m.group(1)) if m else None
    return r.returncode == 0 and pages is not None, pages, _log_summary(r.stdout)


def compile_tex(tex: str, workdir: Path) -> tuple[bool, int | None, str, bool]:
    """pdflatex in workdir (base-resume folder on TEXINPUTS for custom classes).
    -> (ok, pages, log tail, used_font_fallback). Minimal TeX installs (TinyTeX without cm-super) can't render
    some Computer Modern glyphs, e.g. 11pt itemize bullets; that's the machine, not the LaTeX, so retry with
    the near-identical Latin Modern fonts."""
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "resume.tex").write_text(tex)
    if not shutil.which("pdflatex"):
        return True, None, "pdflatex not installed; skipped the compile check", False
    ok, pages, tail = _pdflatex(workdir, "resume.tex")
    fallback = not ok and re.search(r"Font \S+ at \d+ not found", tail) is not None
    if fallback:
        ok, pages, tail = _pdflatex(workdir, FONT_FALLBACK)
    for ext in (".aux", ".log", ".out"):
        (workdir / f"resume{ext}").unlink(missing_ok=True)
    return ok, pages, tail, fallback


_base_checks: dict[str, tuple[bool, int | None, str]] = {}


def check_base(tex: str) -> tuple[bool, int | None, str]:
    """Does the base resume compile on this machine, and how many pages is it? (cached per content)"""
    h = hashlib.sha1(tex.encode()).hexdigest()
    if h not in _base_checks:
        with tempfile.TemporaryDirectory() as d:
            ok, pages, log, _ = compile_tex(tex, Path(d))
        _base_checks[h] = (ok, pages, log)
    return _base_checks[h]


# --------------------------------------------------------------------------- prompt
SYSTEM = """You tailor a candidate's LaTeX resume to one specific job posting.

Truthfulness comes first. Use only facts that are in the base resume or in the candidate's notes. Never \
invent or inflate employers, titles, dates, degrees, GPA, coursework, skills, tools, metrics, team sizes or \
outcomes. You may:
- rephrase a bullet so it surfaces what this posting cares about, using the posting's own terminology where \
it accurately describes the work (ATS keyword matching matters; keyword stuffing does not help)
- reorder sections, entries, bullets and skills so the most relevant material comes first
- tighten wording, merge bullets, and cut or shorten less relevant items to make room
If the posting asks for something the resume doesn't evidence, list it as a gap. Do not add it.

Keep the document compiling and looking the same: keep the preamble, \\documentclass, packages, custom \
commands and environments exactly as they are, and edit only the body. Escape LaTeX special characters \
(& % $ # _ { } ~ ^) in any text you write. Keep the same page count as the base resume (it must not grow).

Keep the candidate's voice: concise, past-tense action-verb bullets, quantified wherever the original was.

Reply in exactly this format, with nothing outside the tags:
<analysis>3-6 short bullets ("- ...") on what this role screens for</analysis>
<changes>short bullets ("- ...") on each edit you made and why</changes>
<gaps>short bullets ("- ...") on requirements the resume doesn't show, with an honest way to address each \
(cover letter, interview story, a quick project); or "- none"</gaps>
<latex>the complete .tex file</latex>"""


def _posting(job: dict, description: str) -> str:
    return (f"Company: {job.get('company', '')}\nRole: {job.get('title', '')}\n"
            f"Location: {', '.join(job.get('locations') or []) or 'n/a'}\n\n{description}")


def _first_message(base_tex: str, notes: str, posting: str) -> dict:
    notes_block = f"<candidate_notes>\n{notes.strip()}\n</candidate_notes>\n\n" if notes.strip() else ""
    return {"role": "user", "content": [
        # The base resume comes first and is cached, so tailoring for several jobs in a row re-reads it cheaply.
        {"type": "text", "text": f"<base_resume>\n{base_tex}\n</base_resume>", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": notes_block + f"<job_posting>\n{posting}\n</job_posting>\n\nTailor the resume to this posting."},
    ]}


def _fix_turns(reply: str, problem: str) -> list[dict]:
    return [{"role": "assistant", "content": reply},
            {"role": "user", "content": problem + "\nReply in the same format with the corrected full file."}]


def _problem(ok: bool, pages: int | None, log: str, base_ok: bool, target: int | None) -> str:
    """What to send back to the model for a fix, or "" if there's nothing it should fix."""
    if not ok:
        # If the machine can't build the original either, it's not the model's error to fix.
        return f"It doesn't compile. pdflatex says:\n{log}" if base_ok else ""
    if target and pages and pages > target:
        return (f"It is {pages} pages but the base resume is {target}. Cut the least relevant material until it "
                f"fits in {target}.")
    return ""


# --------------------------------------------------------------------------- model
def _client():
    import anthropic  # optional dependency; only this feature needs it

    load_env()
    # gzip only: the installed brotli is too old for the SDK's decoder and crashes on br responses.
    return anthropic.Anthropic(default_headers={"Accept-Encoding": "gzip"})


def _ask(client, messages: list[dict], model: str) -> str:
    with client.messages.stream(model=model, max_tokens=32000, system=SYSTEM, messages=messages,
                                output_config={"effort": "medium"}) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError("the model declined this request")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("the reply was cut off (resume too long?)")
    return "".join(b.text for b in msg.content if b.type == "text")


def _tag(text: str, name: str) -> str:
    m = re.search(rf"<{name}>\s*(.*?)\s*</{name}>", text, re.S)
    return m.group(1).strip() if m else ""


def _bullets(s: str) -> list[str]:
    return [re.sub(r"^[-*•]\s*", "", l).strip() for l in s.splitlines() if l.strip() and l.strip() not in ("- none", "none")]


def _latex_from(reply: str) -> str:
    tex = _tag(reply, "latex")
    if not tex:
        raise RuntimeError("the model didn't return LaTeX")
    return re.sub(r"^```(?:latex|tex)?\s*|\s*```$", "", tex)


def _notes_from(reply: str, first: str) -> dict:
    """analysis/changes/gaps from the final reply, falling back to the first one if a fix trimmed them."""
    return {k: _bullets(_tag(reply, k) or _tag(first, k)) for k in ("analysis", "changes", "gaps")}


# --------------------------------------------------------------------------- fix loop
def tailor(job: dict, description: str, base_tex: str, notes: str, workdir: Path, model: str = MODEL,
           stage=lambda s: None) -> dict:
    client = _client()
    base_ok, target, base_log = check_base(base_tex)
    messages = [_first_message(base_tex, notes, _posting(job, description))]
    stage("writing")
    first = reply = _ask(client, messages, model)
    fixes = 0
    while True:
        tex = _latex_from(reply)
        stage("compiling")
        ok, pages, log, fallback = compile_tex(tex, workdir)
        problem = _problem(ok, pages, log, base_ok, target)
        if not problem or fixes >= MAX_FIXES:
            break
        fixes += 1
        stage(f"fixing ({'compile error' if not ok else 'too long'})")
        messages += _fix_turns(reply, problem)
        reply = _ask(client, messages, model)
    return {"latex": tex, "compiled": ok, "pages": pages, "font_fallback": fallback, "base_pages": target,
            "compile_log": "" if ok else log, "base_compiles": base_ok, "base_log": "" if base_ok else base_log,
            "fix_rounds": fixes, **_notes_from(reply, first)}


# --------------------------------------------------------------------------- save results (used by serve.py)
_lock = threading.Lock()


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:30] or "job"


def meta(tid: str) -> dict | None:
    try:
        return json.loads((OUT / tid / "meta.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save(tid: str, d: dict) -> None:
    with _lock:
        p = OUT / tid / "meta.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(p)


def history(limit: int = 50) -> list[dict]:
    rows = [m for d in OUT.glob("*/") if (m := meta(d.name))] if OUT.exists() else []
    rows.sort(key=lambda m: m.get("created", 0), reverse=True)
    return [{k: m.get(k) for k in ("id", "company", "title", "url", "state", "created", "compiled", "pages", "error")}
            for m in rows[:limit]]


def _description_for(job: dict, pasted: str) -> str:
    if pasted.strip():
        return pasted.strip()
    desc = fetch_description(job)
    if len(desc) < 400:
        raise RuntimeError("couldn't read this posting's description; paste it in and retry")
    return desc


def _run(d: dict, job: dict, description: str, notes: str, model: str) -> None:
    tid = d["id"]
    try:
        d["description"] = _description_for(job, description)
        res = tailor(job, d["description"], BASE.read_text(), notes, OUT / tid, model=model,
                     stage=lambda s: _save(tid, {**d, "state": s}))
        d.update(res, state="done", finished=time.time())
    except Exception as e:  # noqa: BLE001
        d.update(state="error", error=str(e)[:500])
    _save(tid, d)


def start(job: dict, description: str = "", notes: str = "", model: str = MODEL) -> str:
    """Kick off a tailoring in a thread; returns its id. Progress and the result land in meta.json."""
    if not BASE.exists():
        raise ValueError("Upload your base resume first (Resume tab)")
    if not has_key():
        raise ValueError("No ANTHROPIC_API_KEY: put it in a .env file in the jobscraper folder, then retry")
    tid = f"{_slug(job.get('company', ''))}-{_slug(job.get('title', ''))}-{int(time.time()) % 10**6}"
    d = {"id": tid, "company": job.get("company", ""), "title": job.get("title", ""), "url": job.get("url"),
         "key": job.get("key"), "state": "fetching description", "created": time.time(), "notes": notes}
    _save(tid, d)
    threading.Thread(target=_run, args=(d, job, description, notes, model), daemon=True, name=f"tailor-{tid}").start()
    return tid
