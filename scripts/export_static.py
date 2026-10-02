"""
Export a read-only static snapshot of the running dashboard (for GitHub Pages).

Crawls every page reachable from the home page of a running `python dashboard.py`,
rewrites links to relative paths, snapshots the live-price / intraday APIs as
JSON so the price charts still work, and disables what needs a server (Search /
Ask AI). Full inbox emails are left out: they are paid newsletter content and
carry per-subscriber unsubscribe links.

    python dashboard.py --no-open           # in another terminal
    python scripts/export_static.py site    # writes ./site
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import os
import posixpath
import re
import sys
import threading
import urllib.parse

import httpx

BASE = "http://127.0.0.1:8765"
REPO_URL = "https://github.com/danieldrewgold/investment-workbench"
SEEDS = ["/", "/fn", "/macro", "/research", "/compare"]

SKIP = [
    re.compile(r"^/research/[^/?]+$"),     # full inbox emails (paid content, personal tokens)
    re.compile(r"/search\?"),              # live search / Ask AI results
    re.compile(r"^/api/"),
]
# Links that identify the subscriber (unsubscribe / tracked redirects in newsletter emails).
PERSONAL_LINK = re.compile(r"substack\.com/redirect|unsubscribe|list-manage\.com|/action/disable_email", re.I)


def page_path(url: str) -> str:
    """Site-relative file path for a dashboard URL."""
    u = urllib.parse.urlsplit(url)
    p = urllib.parse.unquote(u.path).strip("/")
    if p.startswith(("report/", "export/")):
        return p
    segs = [s for s in p.split("/") if s]
    if u.query:
        segs.append("q-" + re.sub(r"[^A-Za-z0-9._-]+", "-", urllib.parse.unquote_plus(u.query)).strip("-"))
    return "/".join(segs + ["index.html"])


def rel(target: str, current: str) -> str:
    r = posixpath.relpath(target, posixpath.dirname(current) or ".")
    return urllib.parse.quote(r, safe="/._-~")


def root_prefix(current: str) -> str:
    depth = current.count("/")
    return "../" * depth


BANNER = ('<div style="position:sticky;top:0;z-index:999;background:#3b2f0b;color:#f5d27a;'
          'font:12px/1.5 system-ui,sans-serif;padding:6px 14px;border-bottom:1px solid #6b5416">'
          'Static snapshot of the Investment Workbench, taken {date}. Prices are frozen and '
          'search / Ask AI are off; everything else is the real output. '
          '<a style="color:#f5d27a;text-decoration:underline" href="{repo}">Source on GitHub</a></div>')

ASK_NOTE = ('<p class="muted" style="margin:4px 0 14px">Search and Ask AI query the full corpus '
            'through a live server, so they are disabled in this static snapshot.</p>')


class Exporter:
    def __init__(self, out: str, base: str = BASE):
        self.out, self.base = out, base
        self.client = httpx.Client(timeout=180.0)
        self.seen: set[str] = set()
        self.lock = threading.Lock()
        self.date = dt.date.today().isoformat()
        self.tickers: set[str] = set()
        self.errors: list[str] = []

    def skip(self, url: str) -> bool:
        return any(rx.search(url) for rx in SKIP)

    def write(self, path: str, data: bytes) -> None:
        full = os.path.join(self.out, *path.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)

    def rewrite(self, html: str, current: str) -> tuple[str, list[str]]:
        found: list[str] = []

        def link(m):
            attr, url = m.group(1), m.group(2)
            url_nofrag, _, frag = url.partition("#")
            if self.skip(url_nofrag):
                return f'{attr}="#" title="not included in the static snapshot"'
            found.append(url_nofrag)
            r = rel(page_path(url_nofrag), current)
            return f'{attr}="{r}{"#" + frag if frag else ""}"'

        html = re.sub(r'(href|action)="(/[^"]*)"', link, html)
        html = re.sub(r'href="([^"]*)"',
                      lambda m: 'href="#"' if PERSONAL_LINK.search(m.group(1)) else m.group(0), html)
        # client-side navigation and API calls
        html = re.sub(r"location='/(co|fn)/'\+([^;]+);", r"location=__R+'\1/'+\2+'/index.html';", html)
        html = html.replace("fetch('/api/quotes')", "fetch(__R+'api/quotes.json')")
        html = html.replace("fetch('/api/intraday?t='+encodeURIComponent(data.ticker))",
                            "fetch(__R+'api/intraday/'+encodeURIComponent(data.ticker)+'.json')")
        html = re.sub(r'<form id="askform".*?</form>', ASK_NOTE, html, flags=re.S)
        html = html.replace("<head>", f'<head><script>var __R="{root_prefix(current)}";</script>', 1)
        html = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + BANNER.format(date=self.date, repo=REPO_URL),
                      html, count=1)
        return html, found

    def fetch(self, url: str) -> list[str]:
        path = page_path(url)
        try:
            r = self.client.get(self.base + url)
        except Exception as e:
            self.errors.append(f"{url}: {type(e).__name__}")
            return []
        if r.status_code != 200:
            self.errors.append(f"{url}: HTTP {r.status_code}")
            return []
        if "text/html" not in r.headers.get("content-type", ""):
            self.write(path, r.content)
            return []
        text = r.text
        m = re.match(r"^/fn/([^/?]+)\?ticker=", url)
        if m:
            # single-ticker drill-down: drop the all-tickers gallery it repeats from /fn/<key>
            text = re.sub(r"<!--fn-gallery-->.*?<!--/fn-gallery-->",
                          f'<p style="margin-top:18px"><a href="/fn/{m.group(1)}">&larr; this output for every ticker</a></p>',
                          text, flags=re.S)
        html, found = self.rewrite(text, path)
        if "<h1>500</h1>" in html:
            self.errors.append(f"{url}: page error")
        self.write(path, html.encode("utf-8"))
        m = re.match(r"^/co/([^/?]+)", url)
        if m:
            with self.lock:
                self.tickers.add(urllib.parse.unquote(m.group(1)))
        return found

    def crawl(self, workers: int = 6) -> None:
        frontier = list(SEEDS)
        with cf.ThreadPoolExecutor(workers) as pool:
            while frontier:
                batch = []
                for u in frontier:
                    if u not in self.seen and not self.skip(u):
                        self.seen.add(u)
                        batch.append(u)
                nxt: list[str] = []
                for found in pool.map(self.fetch, batch):
                    nxt.extend(found)
                frontier = nxt
                print(f"  {len(self.seen)} pages", flush=True)

    def snapshot_apis(self) -> None:
        self.write("api/quotes.json", self.client.get(self.base + "/api/quotes").content)
        for t in sorted(self.tickers):
            r = self.client.get(self.base + "/api/intraday", params={"t": t})
            if r.status_code == 200:
                self.write(f"api/intraday/{t}.json", r.content)

    def finish(self) -> None:
        self.write(".nojekyll", b"")
        self.write("404.html", (
            '<!doctype html><meta charset="utf-8"><title>Not in snapshot</title>'
            '<body style="background:#0d1117;color:#c9d1d9;font:14px system-ui;padding:40px">'
            '<h1>Not in this snapshot</h1><p>This page needs the live server. '
            f'<a style="color:#58a6ff" href="{REPO_URL}">Back to the repo</a></p>').encode())


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "site"
    ex = Exporter(out)
    try:
        ex.client.get(BASE + "/", timeout=10)
    except Exception:
        sys.exit(f"Dashboard not reachable at {BASE}. Start it with: python dashboard.py --no-open")
    ex.crawl()
    ex.snapshot_apis()
    ex.finish()
    print(f"Wrote {len(ex.seen)} pages, {len(ex.tickers)} tickers -> {out}")
    if ex.errors:
        print(f"{len(ex.errors)} problems:")
        for e in ex.errors[:40]:
            print("  ", e)


if __name__ == "__main__":
    main()
