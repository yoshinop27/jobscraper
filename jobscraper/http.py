"""Tiny stdlib HTTP client: JSON GET/POST with retries, backoff on 429/5xx, and per-host concurrency caps."""
from __future__ import annotations

import gzip
import json
import random
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0 Safari/537.36")
PER_HOST = 6  # max concurrent requests to one host
# Shared ATS APIs serve hundreds of boards each, so they get more parallel requests (still polite).
HOST_LIMITS = {"boards-api.greenhouse.io": 16, "api.ashbyhq.com": 12, "api.lever.co": 12, "api.smartrecruiters.com": 8}


class _Limits(dict):
    def __missing__(self, host):
        return self.setdefault(host, threading.Semaphore(HOST_LIMITS.get(host, PER_HOST)))  # atomic: one per host


_host_locks: dict[str, threading.Semaphore] = _Limits()
_cooldown: dict[str, float] = {}  # host -> unix time before which we don't call it (after a 429)


class HTTPError(Exception):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status


def _headers(has_body: bool, extra: dict | None) -> dict:
    hdrs = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9", "Accept-Encoding": "gzip"}
    if has_body:
        hdrs["Content-Type"] = "application/json"
    return {**hdrs, **(extra or {})}


def _wait_for_cooldown(host: str, url: str) -> None:
    wait = _cooldown.get(host, 0) - time.time()
    if wait > 120:
        raise HTTPError(429, url)
    if wait > 0:
        time.sleep(wait)


def _backoff(attempt: int) -> None:
    time.sleep(2 ** attempt + random.random())


def _fetch(url: str, body: bytes | None, headers: dict, timeout: float) -> str:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    with _host_locks[urlparse(url).netloc], urllib.request.urlopen(req, timeout=timeout) as r:
        payload = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            payload = gzip.decompress(payload)
    return payload.decode("utf-8", "replace")


def _parse(text: str, url: str, raw: bool):
    if raw:
        return text
    # Workday answers maintenance windows with an HTML page and status 200
    if "Workday is currently unavailable" in text[:3000]:
        raise HTTPError(503, url + " (Workday maintenance)")
    return json.loads(text)


def request(url: str, *, data: dict | None = None, headers: dict | None = None, timeout: float = 15,
            retries: int = 2, raw: bool = False):
    host = urlparse(url).netloc
    body = json.dumps(data).encode() if data is not None else None
    hdrs = _headers(body is not None, headers)
    for attempt in range(retries + 1):
        can_retry = attempt < retries
        _wait_for_cooldown(host, url)
        try:
            return _parse(_fetch(url, body, hdrs, timeout), url, raw)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _cooldown[host] = time.time() + 30 * (attempt + 1)
            if (e.code == 429 or e.code >= 500) and can_retry:
                _backoff(attempt)
                continue
            raise HTTPError(e.code, url) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if can_retry:
                _backoff(attempt)
                continue
            raise


def get_json(url: str, **kw):
    return request(url, **kw)


def post_json(url: str, data: dict, **kw):
    return request(url, data=data, **kw)


def get_text(url: str, **kw) -> str:
    return request(url, raw=True, **kw)
