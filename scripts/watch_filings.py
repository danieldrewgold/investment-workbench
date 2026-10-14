"""
Filing watcher: notice when a covered name files with the SEC, refresh the free
data that filing touches, and (optionally) republish the public site.

Never spends money. Free mode (core/free_mode.py) blocks every Anthropic call in
this process and in the dashboard it starts for the export, so an unattended run
can't trigger a paid step. Filings that deserve a paid re-run (earnings 8-Ks,
10-Qs, 10-Ks) are flagged in the log and the desktop alert for you to run by hand.

    python scripts/watch_filings.py               # one pass: check, refresh, log
    python scripts/watch_filings.py --publish     # also re-export + push gh-pages when something changed
    python scripts/watch_filings.py --dry-run     # list new filings, change nothing
    python scripts/watch_filings.py --export-only # build the site into data/watcher/site_build, don't push
    python scripts/watch_filings.py --daily-refresh  # also refresh every name's quick panels once a weekday after 8pm
    python scripts/watch_filings.py --loop 15     # keep running, a pass every 15 minutes

The first pass for a ticker records its current filings as a baseline and
triggers nothing. State and the event log live in data/watcher/.
Task Scheduler setup: scripts/install_watcher_task.ps1.
"""

from __future__ import annotations

import datetime as dt
import glob
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from core import free_mode  # noqa: E402

free_mode.enable()          # before anything else can import an LLM caller

import httpx  # noqa: E402

from ingestion.loaders._edgar_utils import SEC_HEADERS  # noqa: E402

WATCH_DIR = os.path.join("data", "watcher")
STATE = os.path.join(WATCH_DIR, "state.json")
EVENTS = os.path.join(WATCH_DIR, "events.jsonl")
LOCK = os.path.join(WATCH_DIR, "watcher.lock")
HIDDEN_STASH = os.path.join(WATCH_DIR, "_hidden")
BUILD = os.path.join(WATCH_DIR, "site_build")

# Names kept off the public site. Their files are moved aside during the export.
HIDDEN_FROM_SITE = {"GIII"}
EXPORT_PORT = 8799
PUBLISH_MIN_GAP_H = 1.0     # batch bursts of filings into one push
PUBLISH_MAX_AGE_H = 24.0    # republish daily even with no filings (prices, macro)
LIGHT_REFRESH_HOUR = 20     # --daily-refresh: quick panels for every name once a weekday, at the evening run
SEEN_KEEP = 200
QUIET_FORMS = {"4", "4/A", "144", "144/A", "3", "5"}   # logged and refreshed, no desktop alert

# 8-K item -> free steps it changes. Items not listed still refresh the press panel.
ITEM_STEPS = {
    "2.02": ["consensus", "market_overlay", "press_releases", "quarterly_financials"],   # earnings
    "2.05": ["workforce_signal", "press_releases"],                                        # restructuring
    "1.01": ["press_releases"], "2.01": ["press_releases", "financials"],
    "5.02": ["press_releases"], "7.01": ["press_releases"], "8.01": ["press_releases"],
    "2.03": ["bond_health"], "2.04": ["bond_health"],
}
FORM_STEPS = {
    "10-Q": ["financials", "quarterly_financials", "bond_health"],
    "10-K": ["financials", "quarterly_financials", "bond_health", "peer_comps"],
    "20-F": ["financials", "quarterly_financials"],
    "40-F": ["financials", "quarterly_financials"],
    "6-K": ["press_releases", "quarterly_financials"],
    "4": ["filing_form4"],
    "SC 13D": ["filing_13d", "ownership_holders"], "SCHEDULE 13D": ["filing_13d", "ownership_holders"],
    "SC 13G": ["ownership_holders"], "SCHEDULE 13G": ["ownership_holders"],
}
# Filings that change the thesis inputs: the alert suggests a paid re-run.
PAID_WORTHY_ITEMS = {"2.02"}
PAID_WORTHY_FORMS = {"10-Q", "10-K", "20-F", "40-F"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _log(msg: str) -> None:
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(WATCH_DIR, "watcher.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _tickers() -> list[str]:
    return sorted({os.path.basename(f).split("_")[0]
                   for f in glob.glob(os.path.join("data", "results", "*.json"))} - {""})


def _has_call(ticker: str) -> bool:
    """True if the name has a call-layer report (re-runnable with rerun_call.py)."""
    return bool(glob.glob(os.path.join("data", "reports", f"{ticker}_*_digest.md")))


def notify(title: str, body: str) -> None:
    """Windows desktop toast, best effort (text passed via env, no quoting issues)."""
    if os.name != "nt":
        return
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x = $t.GetElementsByTagName('text');"
        "$x.Item(0).AppendChild($t.CreateTextNode($env:WB_TITLE)) > $null;"
        "$x.Item(1).AppendChild($t.CreateTextNode($env:WB_BODY)) > $null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       env={**os.environ, "WB_TITLE": title[:120], "WB_BODY": body[:400]},
                       timeout=30, capture_output=True)
    except Exception:
        pass


# --------------------------------------------------------------------------
# EDGAR polling
# --------------------------------------------------------------------------

def _cik_map(client: httpx.Client) -> dict[str, str]:
    r = client.get("https://www.sec.gov/files/company_tickers.json", timeout=30)
    r.raise_for_status()
    return {e["ticker"].upper(): str(e["cik_str"]).zfill(10) for e in r.json().values()}


def _recent_filings(client: httpx.Client, cik: str) -> list[dict]:
    r = client.get(f"https://data.sec.gov/submissions/CIK{cik}.json", timeout=30)
    r.raise_for_status()
    rec = (r.json().get("filings") or {}).get("recent") or {}
    keys = ["accessionNumber", "form", "filingDate", "acceptanceDateTime", "items", "primaryDocument"]
    cols = [rec.get(k) or [] for k in keys]
    out = []
    for row in zip(*cols):
        d = dict(zip(keys, row))
        acc = d["accessionNumber"]
        d["url"] = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                    f"{acc.replace('-', '')}/{d['primaryDocument']}")
        out.append(d)
    return out[:SEEN_KEEP]


def classify(f: dict) -> tuple[list[str], bool, str]:
    """(steps to refresh, paid re-run worthwhile, short label) for one filing."""
    form = f["form"].removesuffix("/A")
    items = [i.strip() for i in (f.get("items") or "").split(",") if i.strip()]
    steps: list[str] = []
    paid = False
    if form == "8-K":
        for it in items:
            steps += ITEM_STEPS.get(it, ["press_releases"])
            paid |= it in PAID_WORTHY_ITEMS
        steps = steps or ["press_releases"]
        label = "8-K " + ",".join(items) + (" (earnings)" if "2.02" in items else "")
    else:
        steps = list(FORM_STEPS.get(form, []))
        paid = form in PAID_WORTHY_FORMS
        label = f["form"]
    return list(dict.fromkeys(steps)), paid, label


def scan(tickers: list[str], state: dict) -> list[dict]:
    """New filings across all tickers since the last pass. Updates state in place."""
    events = []
    with httpx.Client(headers=SEC_HEADERS) as client:
        ciks = _cik_map(client)
        for t in tickers:
            cik = ciks.get(t) or ciks.get(t.replace(".", "-"))
            if not cik:
                continue                                    # no SEC filer (e.g. OTC ADR)
            try:
                filings = _recent_filings(client, cik)
            except Exception as e:
                _log(f"{t}: EDGAR error {type(e).__name__}")
                continue
            finally:
                time.sleep(0.15)                            # SEC asks for <= 10 req/s
            st = state.setdefault(t, {})
            seen = set(st.get("seen") or [])
            first = not st.get("seen")
            for f in filings:
                if f["accessionNumber"] in seen or first:
                    continue
                steps, paid, label = classify(f)
                events.append({"ts": _now(), "ticker": t, "form": f["form"], "items": f.get("items") or "",
                               "label": label, "filed": f["filingDate"], "accepted": f.get("acceptanceDateTime"),
                               "accession": f["accessionNumber"], "url": f["url"],
                               "steps": steps, "paid_suggested": paid})
            st["seen"] = [f["accessionNumber"] for f in filings]
            st["checked"] = _now()
            if first:
                _log(f"{t}: baseline recorded ({len(filings)} filings)")
    return events


# --------------------------------------------------------------------------
# free refresh
# --------------------------------------------------------------------------

def refresh_steps(by_ticker: dict[str, list[str]]) -> dict[str, list[str]]:
    import refresh_data
    from research.company_registry import COMPANY_REGISTRY
    done = {}
    for t, steps in by_ticker.items():
        steps = [s for s in steps if s in refresh_data.FREE_STEPS]
        if steps:
            done[t] = refresh_data.refresh(t, COMPANY_REGISTRY, steps)
    return done


# --------------------------------------------------------------------------
# publish (export with hidden names moved aside, then force-push gh-pages)
# --------------------------------------------------------------------------

def _hidden_paths(t: str) -> list[str]:
    pats = [f"data/results/{t}_*", f"data/dag_cache/{t}", f"data/reports/{t}_*",
            f"data/exports/{t}_*", f"data/dag_traces/{t}_*"]
    return [p for pat in pats for p in glob.glob(pat)]


def restore_hidden() -> None:
    """Put back anything a previous (possibly crashed) publish moved aside."""
    if not os.path.isdir(HIDDEN_STASH):
        return
    conflicts = []
    for dirpath, _, filenames in os.walk(HIDDEN_STASH):
        for fn in filenames:
            src = os.path.join(dirpath, fn)
            dst = os.path.relpath(src, HIDDEN_STASH)
            if os.path.exists(dst):
                conflicts.append(dst)                       # never overwrite; leave it stashed
                continue
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            shutil.move(src, dst)
    if conflicts:
        _log(f"restore: {len(conflicts)} stashed files already exist, left in {HIDDEN_STASH} (e.g. {conflicts[0]})")
    else:
        shutil.rmtree(HIDDEN_STASH, ignore_errors=True)


def _stash_hidden() -> dict:
    quotes_path = os.path.join("data", "quotes.json")
    removed = {}
    for t in HIDDEN_FROM_SITE:
        for p in _hidden_paths(t):
            dst = os.path.join(HIDDEN_STASH, p)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(p, dst)
    q = _load_json(quotes_path, None)
    if isinstance(q, dict) and isinstance(q.get("quotes"), dict):
        removed = {t: q["quotes"].pop(t) for t in HIDDEN_FROM_SITE if t in q["quotes"]}
        _save_json(quotes_path, q)
    return removed


def _unstash_hidden(removed_quotes: dict) -> None:
    restore_hidden()
    quotes_path = os.path.join("data", "quotes.json")
    q = _load_json(quotes_path, None)
    if removed_quotes and isinstance(q, dict) and isinstance(q.get("quotes"), dict):
        for t, v in removed_quotes.items():
            q["quotes"].setdefault(t, v)
        _save_json(quotes_path, q)


def _wait_up(url: str, secs: int = 120) -> bool:
    end = time.time() + secs
    while time.time() < end:
        try:
            if httpx.get(url, timeout=60).status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def _export(out: str) -> int:
    spec = importlib.util.spec_from_file_location("export_static", os.path.join("scripts", "export_static.py"))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ex = mod.Exporter(out, base=f"http://127.0.0.1:{EXPORT_PORT}")
    ex.crawl()
    ex.snapshot_apis()
    ex.finish()
    if ex.errors:
        _log(f"export: {len(ex.errors)} problems (first: {ex.errors[0]})")
    return len(ex.seen)


TEXT_EXTS = {".html", ".json", ".md", ".txt", ".csv", ".css", ".js", ".svg", ".xml", ".nojekyll", ""}
ZIP_EXTS = {".docx", ".xlsx", ".pptx"}


def _leaks(out: str) -> list[str]:
    """Files that mention a hidden ticker (case-insensitive, URL-encoded too), or that
    can't be scanned. Office files are read member by member. Fails closed."""
    import urllib.parse
    import zipfile
    needles = {t.lower() for t in HIDDEN_FROM_SITE} | {urllib.parse.quote(t).lower() for t in HIDDEN_FROM_SITE}
    hit = lambda s: any(n in s.lower() for n in needles)   # noqa: E731
    hits = []
    for dirpath, dirnames, files in os.walk(out):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in files:
            p = os.path.join(dirpath, fn)
            ext = os.path.splitext(fn)[1].lower()
            try:
                if hit(os.path.relpath(p, out)):
                    hits.append(p)
                elif ext in TEXT_EXTS:
                    if hit(open(p, encoding="utf-8", errors="ignore").read()):
                        hits.append(p)
                elif ext in ZIP_EXTS:
                    with zipfile.ZipFile(p) as z:
                        if any(hit(m) or hit(z.read(m).decode("utf-8", "ignore")) for m in z.namelist()):
                            hits.append(p)
                else:
                    hits.append(p + " (unscannable type)")
            except Exception as e:
                hits.append(f"{p} (scan error {type(e).__name__})")
    return hits


def _git(args: list[str], cwd: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def publish(state: dict, *, push: bool = True) -> bool:
    import socket
    with socket.socket() as s:                              # a server already on the port (e.g. a local
        if s.connect_ex(("127.0.0.1", EXPORT_PORT)) == 0:   # dashboard with GIII) must not be exported
            raise RuntimeError(f"port {EXPORT_PORT} is already in use; not exporting")
    shutil.rmtree(BUILD, ignore_errors=True)
    removed = _stash_hidden()
    dash = None
    try:
        log = open(os.path.join(WATCH_DIR, "export_dashboard.log"), "w", encoding="utf-8")
        dash = free_mode.run_free("dashboard.py", "--no-open", "--port", str(EXPORT_PORT),
                                  stdout=log, stderr=subprocess.STDOUT)
        if not _wait_up(f"http://127.0.0.1:{EXPORT_PORT}/") or dash.poll() is not None:
            raise RuntimeError("dashboard did not start (see data/watcher/export_dashboard.log)")
        pages = _export(BUILD)
        if dash.poll() is not None:
            raise RuntimeError("export dashboard exited mid-export; not pushing")
    finally:
        if dash:
            dash.terminate()
            try:
                dash.wait(20)
            except Exception:
                dash.kill()
        _unstash_hidden(removed)

    prev = int(state.get("_publish", {}).get("pages") or 0)
    if prev and pages < 0.8 * prev:
        raise RuntimeError(f"export shrank to {pages} pages from {prev}; not pushing")
    leaks = _leaks(BUILD)
    if leaks:
        raise RuntimeError(f"hidden ticker found in {len(leaks)} files (e.g. {leaks[0]}); not pushing")
    if not push:
        _log(f"exported {pages} pages to {BUILD} (not pushed)")
        return True

    remote = _git(["remote", "get-url", "origin"], ROOT)
    _git(["init", "-q", "-b", "gh-pages"], BUILD)
    _git(["add", "-A"], BUILD)
    _git(["-c", "user.name=workbench-watcher", "-c", "user.email=watcher@localhost",
          "commit", "-q", "-m", f"site snapshot {_now()}"], BUILD)
    _git(["push", "-q", "--force", remote, "gh-pages:gh-pages"], BUILD)
    state["_publish"] = {"at": _now(), "pages": pages}
    _log(f"published {pages} pages to gh-pages")
    return True


# --------------------------------------------------------------------------
# one pass
# --------------------------------------------------------------------------

def run_pass(*, do_publish: bool, dry_run: bool, daily_refresh: bool = False) -> None:
    os.makedirs(WATCH_DIR, exist_ok=True)
    restore_hidden()
    state = _load_json(STATE, {})
    tickers = _tickers()
    events = scan(tickers, state)
    _log(f"checked {len(tickers)} names: {len(events)} new filings")
    for e in events:
        _log(f"  {e['ticker']:6s} {e['label']:28s} filed {e['filed']}  -> {', '.join(e['steps']) or '(no panel)'}"
             + ("  [paid re-run suggested]" if e["paid_suggested"] else ""))
    if dry_run:
        return

    by_ticker: dict[str, list[str]] = {}
    for e in events:
        by_ticker.setdefault(e["ticker"], [])
        by_ticker[e["ticker"]] += [s for s in e["steps"] if s not in by_ticker[e["ticker"]]]
    done = refresh_steps(by_ticker) if by_ticker else {}

    with open(EVENTS, "a", encoding="utf-8") as f:
        for e in events:
            e["refreshed"] = done.get(e["ticker"], [])
            f.write(json.dumps(e) + "\n")
    if done:
        state["_dirty"] = True
    _save_json(STATE, state)

    paid = sorted({e["ticker"] for e in events if e["paid_suggested"]})
    alertable = [e for e in events if e["form"] not in QUIET_FORMS]
    if alertable:
        body = "; ".join(f"{e['ticker']} {e['label']}" for e in alertable[:6])
        if paid:
            body += " | re-run: " + ", ".join(
                f"{t} ({'rerun_call.py' if _has_call(t) else 'full pipeline'})" for t in paid)
        notify(f"{len(alertable)} new filing(s)", body)

    now = dt.datetime.now()
    if daily_refresh and now.weekday() < 5 and now.hour >= LIGHT_REFRESH_HOUR \
            and state.get("_light_refresh") != now.date().isoformat():
        import refresh_data
        from research.company_registry import COMPANY_REGISTRY
        _log(f"daily light refresh: {len(tickers)} names")
        for t in tickers:
            refresh_data.refresh(t, COMPANY_REGISTRY, refresh_data.LIGHT_STEPS)
        state["_light_refresh"] = now.date().isoformat()
        state["_dirty"] = True
        _save_json(STATE, state)

    if do_publish:
        last = state.get("_publish", {}).get("at")
        age_h = (dt.datetime.now() - dt.datetime.fromisoformat(last)).total_seconds() / 3600 if last else 1e9
        if (state.get("_dirty") and age_h > PUBLISH_MIN_GAP_H) or age_h > PUBLISH_MAX_AGE_H:
            try:
                publish(state)
                state["_dirty"] = False
            except Exception as e:
                _log(f"publish failed: {e}")
                notify("Site publish failed", str(e))
            _save_json(STATE, state)


def main() -> None:
    os.makedirs(WATCH_DIR, exist_ok=True)
    if sys.stdout is None:                                  # pythonw (Task Scheduler): no console at all
        sys.stdout = sys.stderr = open(os.devnull, "w")     # _log still writes data/watcher/watcher.log
    args = sys.argv[1:]
    do_publish, dry_run = "--publish" in args, "--dry-run" in args
    daily_refresh = "--daily-refresh" in args
    if "--export-only" in args:
        restore_hidden()
        publish(_load_json(STATE, {}), push=False)
        return
    loop_min = float(args[args.index("--loop") + 1]) if "--loop" in args else 0

    os.makedirs(WATCH_DIR, exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        if time.time() - os.path.getmtime(LOCK) < 3 * 3600:
            _log("another watcher pass is running; exiting")
            return
        os.remove(LOCK)                                     # stale lock from a crashed run
        return main()
    try:
        while True:
            try:
                run_pass(do_publish=do_publish, dry_run=dry_run, daily_refresh=daily_refresh)
            except Exception as e:
                _log(f"pass failed: {type(e).__name__}: {e}")
            if not loop_min:
                break
            os.utime(LOCK)
            time.sleep(loop_min * 60)
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass


if __name__ == "__main__":
    main()
