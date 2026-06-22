"""
IR Press Loader

Pull press-release links from a company's own INVESTOR-RELATIONS / newsroom
site (nicer UI than raw EDGAR exhibits) — earnings releases AND product / company
news, run through the dashboard's importance filter.

IR sites are heterogeneous, so this is layered + best-effort:
  1. find_ir_url(ticker) gives a starting IR URL (often right, sometimes a stale
     pattern-guess), from which we derive the base domain.
  2. Build candidate listing pages across host variants (ir./investor./newsroom./
     news., and <domain>/newsroom, /news, /press-releases).
  3. For each: discover RSS feeds (clean title/date/link — Q4 `?pagetemplate=rss`,
     Apple `/newsroom/rss-feed.rss`, NVIDIA `/cats/press_release.xml`, on-page
     <link rel=alternate> and referenced .xml/.rss) and scrape dated release links
     from the HTML. WAF-blocked (403) or SPA pages are re-fetched with the
     Playwright browser worker (system Chrome bypasses WAF), like the deck loader.
  4. Merge, dedupe, date-parse, newest-first.

EDGAR (press_releases step) remains the complete 3-year fallback.
Returns {ticker, ir_url, feeds, items:[{title,date,url,source}], fetched_at, error}.
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import httpx

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# Press-release permalinks embed a date in several shapes; also accept /news/,
# /press-release/, /newsroom/ paths.
_URL_DATE_RES = [
    re.compile(r"/((?:19|20)\d\d)-(\d\d)-(\d\d)-"),     # Q4: /2026-06-16-Headline
    re.compile(r"/((?:19|20)\d\d)/(\d\d)/(\d\d)[/-]"),  # /2026/06/16/...
    re.compile(r"/((?:19|20)\d\d)/(\d\d)/"),            # Apple: /newsroom/2026/06/slug
]
_PRESS_PATH_RE = re.compile(r"(press[-_]?release|news[-_]?release|/news/|/press/|/newsroom/)", re.I)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_RSS_LINK_RE = re.compile(r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', re.I)
_RSS_SUFFIXES = ["?pagetemplate=rss", "/rss", "/feed", "/feed/", "/rss-feed.rss",
                 "/rss/news-releases.xml", "/cats/press_release.xml", "/rss.xml"]
_NEWS_PATHS = ["news", "press-releases", "news-releases", "newsroom",
               "news-and-events/news", "news-and-events/press-releases",
               "investor-news", "press"]


def _clean(s: str) -> str:
    return _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _decode(b: bytes) -> str:
    """Decode bytes robustly: declared charset, else UTF-8 strict, else cp1252."""
    m = re.search(rb'encoding=["\']([\w-]+)["\']', b[:300]) or \
        re.search(rb'charset=["\']?([\w-]+)', b[:1500])
    if m:
        try:
            return b.decode(m.group(1).decode("ascii", "ignore"), "replace")
        except Exception:
            pass
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp1252", "replace")


def _date_from(url: str, text: str = "") -> str:
    for rx in _URL_DATE_RES:
        m = rx.search(url or "")
        if m:
            g = m.groups()
            return f"{g[0]}-{g[1]}-{(g[2] if len(g) > 2 else '01')}"
    m = re.search(r"((?:19|20)\d\d)[-/](\d\d?)[-/](\d\d?)", text or "")
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"([A-Z][a-z]{2,8})\.?\s+(\d{1,2}),?\s+((?:19|20)\d\d)", text or "")
    if m:
        try:
            return datetime.strptime(f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}", "%b %d %Y").strftime("%Y-%m-%d")
        except Exception:
            pass
    return ""


def _base_domain(url: str) -> str:
    host = urlparse(url).netloc.split(":")[0]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])      # foo.co.uk
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _fetch(url: str, *, allow_browser: bool = True, verbose: bool = False) -> str | None:
    """GET decoded text. On 403 (WAF) or an empty/SPA shell, retry with the
    Playwright browser (system Chrome) which bypasses most WAFs and renders SPAs."""
    status, txt = None, None
    try:
        r = httpx.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
        status = r.status_code
        if status == 200:
            txt = _decode(r.content)
            looks_ok = ("<item" in txt or "<entry" in txt
                        or len(re.findall(r"<a\b", txt)) > 25)
            if looks_ok:
                return txt
    except Exception:
        pass
    if allow_browser and (status in (403, 401, 429, None) or not txt):
        try:
            from ingestion.loaders._browser_fetch import fetch_html_with_browser
            bt = fetch_html_with_browser(url, post_load_wait_ms=2800, verbose=verbose)
            if bt:
                return bt
        except Exception:
            pass
    return txt


def _parse_rss(xml: str, base: str) -> list[dict]:
    out = []
    for blk in re.findall(r"<item\b.*?</item>|<entry\b.*?</entry>", xml or "", re.S | re.I):
        t = re.search(r"<title[^>]*>(.*?)</title>", blk, re.S | re.I)
        title = _clean(re.sub(r"<!\[CDATA\[|\]\]>", "", t.group(1)) if t else "")
        l = re.search(r"<link[^>]*>(.*?)</link>", blk, re.S | re.I)
        link = _clean(l.group(1)) if l else ""
        if not link:
            la = re.search(r'<link[^>]+href=["\']([^"\']+)["\']', blk, re.I)
            link = la.group(1) if la else ""
        d = re.search(r"<(?:pubDate|published|updated|dc:date)[^>]*>(.*?)</", blk, re.S | re.I)
        date = ""
        if d:
            raw = _clean(d.group(1))
            try:
                date = parsedate_to_datetime(raw).strftime("%Y-%m-%d")
            except Exception:
                date = _date_from(link, raw) or (raw[:10] if re.match(r"\d{4}-\d\d-\d\d", raw) else "")
        if title and link and len(title) > 8:
            out.append({"title": title[:200], "date": date or _date_from(link),
                        "url": urljoin(base, link), "source": "IR · RSS"})
    return out


def _items_from_html(html_text: str, base: str) -> list[dict]:
    items, seen = [], set()
    for a in re.findall(r"<a\b[^>]*>.*?</a>", html_text or "", re.I | re.S):
        m = _HREF_RE.search(a)
        if not m:
            continue
        href = m.group(1)
        if href in seen or re.search(r"(\?pagetemplate|/rss|\.xml|/category|/tag/|/page/|#|mailto:|javascript:)", href, re.I):
            continue
        if re.search(r"//blogs?\.|/blog/|/community/", href, re.I):  # blogs aren't press releases
            continue
        date = _date_from(href, a)
        if not (date or _PRESS_PATH_RE.search(href)):
            continue
        title = (re.search(r'title=["\']([^"\']+)["\']', a) or [None, ""])[1] if re.search(r'title=["\']', a) else ""
        title = _clean(title) or _clean(a)
        if len(title) < 14:
            continue
        if not date:
            continue   # require a date so listing/nav links don't slip in
        seen.add(href)
        items.append({"title": title[:200], "date": date,
                      "url": urljoin(base, href), "source": "IR · site"})
    return items


def _discover_feeds(html_text: str, base: str) -> list[str]:
    feeds = []
    for tag in _RSS_LINK_RE.findall(html_text or ""):
        m = _HREF_RE.search(tag)
        if m:
            feeds.append(urljoin(base, m.group(1)))
    for href in _HREF_RE.findall(html_text or ""):
        if re.search(r"\.(xml|rss)(\?|$)|/rss|/feed|pagetemplate=rss", href, re.I) \
                and not re.search(r"sitemap|comment", href, re.I):
            feeds.append(urljoin(base, href))
    return list(dict.fromkeys(feeds))


def _candidate_pages(ir_url: str, base: str) -> list[str]:
    """News-listing URLs to try, HIGHEST-VALUE FIRST (so the cap doesn't drop the
    good page). News paths resolve against the IR HOST ROOT and the main domain —
    the cached ir_url is often a DEEP page (…/detailed-stock-quote), so appending
    to it yields wrong URLs."""
    c = []
    if ir_url:
        p = urlparse(ir_url)
        root = f"{p.scheme}://{p.netloc}"
        c += [ir_url, root + "/"]
        for path in ("news", "press-releases", "newsroom", "news-releases",
                     "news-and-events/press-releases", "news-and-events/news",
                     "investor-relations/news", "investor-news"):
            c.append(f"{root}/{path}")
    # main-domain newsrooms (Apple, NVIDIA style)
    for h in (f"https://www.{base}", f"https://{base}"):
        for path in ("newsroom", "news"):
            c.append(f"{h}/{path}")
    # subdomain variants
    for sub in ("newsroom", "news", "ir", "investor", "investors"):
        rt = f"https://{sub}.{base}"
        c += [rt + "/", f"{rt}/news", f"{rt}/press-releases"]
    # deeper ir_url-relative (lowest value)
    if ir_url:
        for path in _NEWS_PATHS:
            c.append(urljoin(ir_url if ir_url.endswith("/") else ir_url + "/", path))
    return list(dict.fromkeys(c))[:32]


def fetch_ir_press(ticker: str, *, max_items: int = 60, verbose: bool = False) -> dict:
    out = {"ticker": ticker.upper(), "ir_url": "", "feeds": [], "items": [],
           "fetched_at": datetime.now().isoformat(timespec="seconds"), "error": ""}
    try:
        from ingestion.loaders._ir_page_finder import find_ir_url
        ir = find_ir_url(ticker, verbose=verbose)
        ir_url = getattr(ir, "url", "") or ""
    except Exception as e:
        out["error"] = f"find_ir_url: {type(e).__name__}: {e}"
        ir_url = ""
    out["ir_url"] = ir_url
    base = _base_domain(ir_url) if ir_url else ""
    if not base:
        out["error"] = out["error"] or "no IR url / base domain"
        return out

    cand = _candidate_pages(ir_url, base)
    is_news = re.compile(r"news|press|newsroom", re.I)

    def _collect(pages, allow_browser):
        disc, guess, html_items = [], [], []
        for page in pages:
            h = _fetch(page, allow_browser=allow_browser, verbose=verbose)
            if not h:
                continue
            disc += _discover_feeds(h, page)
            if is_news.search(page):
                guess += [page.rstrip("/") + s for s in _RSS_SUFFIXES]
            html_items += _items_from_html(h, page)
        feed_items = []
        for fu in list(dict.fromkeys(disc + guess))[:20]:
            xml = _fetch(fu, allow_browser=False)
            if xml and ("<item" in xml or "<entry" in xml):
                got = _parse_rss(xml, fu)
                if got:
                    out["feeds"].append(fu)
                    feed_items += got
                    if verbose:
                        print(f"  [IRpress] RSS {fu} -> {len(got)}")
        return html_items + feed_items

    # Pass 1: httpx across all candidates (order-independent). Pass 2: if that
    # surfaced too little (common — most IR sites are SPAs / WAF-gated), render
    # the IR homepage + top news candidates with the browser (system Chrome).
    items = _collect(cand, allow_browser=False)
    if len(items) < 3:
        bpages = ([ir_url] if ir_url else []) + [p for p in cand if is_news.search(p)][:5]
        items += _collect(list(dict.fromkeys(bpages)), allow_browser=True)

    cutoff = str(datetime.now().year - 4)   # drop stale strays (e.g. old blog posts)
    seen, merged = set(), []
    for it in sorted(items, key=lambda x: x.get("date", ""), reverse=True):
        if it.get("date") and it["date"][:4] < cutoff:
            continue
        key = re.sub(r"[?#].*$", "", it["url"].rstrip("/")).lower()
        tk = (it.get("title") or "")[:60].lower()
        if key in seen or tk in seen:
            continue
        seen.add(key)
        seen.add(tk)
        merged.append(it)
    out["items"] = merged[:max_items]
    out["feeds"] = list(dict.fromkeys(out["feeds"]))
    if verbose:
        print(f"  [IRpress] {ticker}: {len(out['items'])} items, {len(out['feeds'])} feed(s)")
    return out
