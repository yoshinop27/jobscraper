# Internship Radar

Finds SWE / ML / data / quant internships **the moment they're posted on company job boards**, hours before
the community lists (Simplify, GitHub internship repos) pick them up.

On my machine it watches **2,637 company boards**. Among the postings it shares with the lists, it has seen
262 first, by a median of **2.8 hours**.

## How it works

```
 company job boards ──┐                       ┌── dashboard (localhost:8787)
 (Greenhouse, Lever,  │   workers ──► filters ──► SQLite ──┤   browse · apply · hide · stats
  Ashby, Workday, …)  ├─► poll on a                         ├── macOS / phone notifications
 big-tech careers APIs│   schedule                          └── JSON API (/api/jobs)
 Simplify/GitHub lists┘
```

1. **Discover** (`companies.py`). Builds the board list from the ATS links in Simplify's full posting history,
   plus hand-checked seeds, then assigns each company a tier: FAANG+, quant, unicorn, high-pay or other.
2. **Poll** (`worker.py`, `poller.py`). Three background lanes:
   - **fast** — tiered companies, every few minutes;
   - **slow** — every other board;
   - **lists** — the Simplify and GitHub lists.

   Fetches run in parallel, with at most 6 concurrent requests per host and backoff on HTTP 429.
3. **Fetch** (`sources/`). One fetcher per source turns each API's response into the same posting shape:
   the public JSON APIs of Greenhouse, Lever, Ashby, SmartRecruiters and Workday, and the careers APIs of
   Amazon, Google, TikTok, Netflix and Microsoft.
4. **Filter** (`filters.py`). Keeps a posting only if it:
   - is an internship or co-op;
   - is a SWE, ML, data or quant role;
   - isn't PhD/MS/MBA-only;
   - is for the right season;
   - is in a target city or US-remote;
   - pays at least the minimum when pay is listed.

   Every rejection is stored with its reason.
5. **Serve** (`serve.py`, `dashboard.html`). A standard-library HTTP server that starts and stops the workers,
   shows matches, and tracks which jobs you applied to.

The lists are still ingested for two reasons. They cover companies that block automated access, and they're
the benchmark for the "beat the lists by N hours" number.

**Resume tailoring** (`tailor.py`, optional). Click *Tailor* on a job and Claude rewrites your LaTeX resume for
that posting. It may only reorder, rephrase or cut what the resume already says; anything missing is listed as
a gap. The result is compiled with `pdflatex`, and compile errors go back to the model to fix.

## Quick start

Python 3.11+, standard library only.

```bash
python3 -m jobscraper discover   # build the board list (~1 min)
python3 -m jobscraper serve      # http://localhost:8787 — press Start to begin polling
```

Settings (cities, roles, minimum pay, poll intervals) are in `config.toml` and can also be changed in the
dashboard.

Other commands:

| Command | What it does |
|---|---|
| `list [--all]` | Prints current matches in the terminal |
| `stats` | Board health and lead time over the lists |
| `companies` | Shows which companies are polled, by tier |
| `rescore` | Re-applies the filters after you edit `config.toml` |
| `export jobs.csv` | Writes matches to a CSV file |
| `poll --only stripe` | Polls one company now, for debugging |

**Phone alerts.** Set `ntfy_topic` in `config.toml` and subscribe to that topic in the free ntfy app. Only the
company, title, location and link are sent.

**Resume tailoring.** Put `ANTHROPIC_API_KEY=...` in `.env`, then run `pip install anthropic`.

**Run in the background on macOS:**

```bash
sed -e "s|REPO|$PWD|g" -e "s|PYTHON|$(which python3)|g" launchd/com.jobscraper.serve.plist \
  > ~/Library/LaunchAgents/com.jobscraper.serve.plist
launchctl load ~/Library/LaunchAgents/com.jobscraper.serve.plist
```

**Docker:**

```bash
docker build -t jobscraper .
docker run -p 127.0.0.1:8787:8787 -v "$PWD/data:/app/data" jobscraper
```

## Tests

```bash
python3 -m pytest -q
```

## Being a good citizen

It uses only public, unauthenticated endpoints. It limits concurrent requests per host and backs off when a
site rate-limits it. It fetches full descriptions only for new postings that already passed the title filter.
Sites with bot protection are skipped, never bypassed. Handshake isn't included, because it sits behind a
school login.
