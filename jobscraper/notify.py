"""New-posting alerts: macOS notification center and, optionally, ntfy.sh phone push."""
from __future__ import annotations

import subprocess
import sys
import urllib.request


MAX_INDIVIDUAL_ALERTS = 5


def _osa(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def macos(title: str, body: str) -> None:
    if sys.platform != "darwin":
        return
    script = f'display notification "{_osa(body)}" with title "{_osa(title)}" sound name "Glass"'
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def ntfy(topic: str, title: str, body: str, url: str | None = None) -> None:
    # HTTP headers must be ASCII, so emoji etc. are dropped from the title
    headers = {"Title": title.encode("ascii", "ignore").decode(), "Priority": "high", "Tags": "briefcase"}
    if url:
        headers["Click"] = url
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode(), headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # noqa: BLE001
        print(f"[notify] ntfy failed: {e}", file=sys.stderr)


def _summary(jobs: list[dict]) -> dict:
    return {"company": f"{len(jobs)} new internships", "title": ", ".join(sorted({j["company"] for j in jobs})[:8])}


def _body(job: dict) -> str:
    parts = [job["title"]] + [job[k] for k in ("loc", "pay") if job.get(k)]
    return " · ".join(map(str, parts))


def send(cfg: dict, jobs: list[dict]) -> None:
    """One alert per job when there are a few, one summary when there are many."""
    if not jobs:
        return
    settings = cfg.get("notify", {})
    for j in jobs if len(jobs) <= MAX_INDIVIDUAL_ALERTS else [_summary(jobs)]:
        title, body = f"🆕 {j['company']}", _body(j)
        if settings.get("macos", True):
            macos(title, body)
        if settings.get("ntfy_topic"):
            ntfy(settings["ntfy_topic"], title, body, j.get("url"))
