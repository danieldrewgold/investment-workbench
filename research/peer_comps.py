"""
Peer Comps Loader.

For a given subject ticker + schema, fetches forward consensus for a
curated peer group and produces a compact comparison table. Previously
the brief would make claims like "WING SSS is weaker than peers" with
zero peer data behind it — this gives Claude concrete peer numbers to
cite.

Public API:
    fetch_peer_comps(subject_ticker, schema_type,
                      max_peers=4, verbose=False) -> PeerComps

Each peer row carries forward revenue growth, forward EPS growth,
NTM P/E (from mean PT + forward EPS), and recent revision activity —
all from the existing `fetch_consensus()` loader (no new API dependency).

Cached per-peer per-day to stay polite to yfinance.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


from research.peer_registry import peers_for


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------

@dataclass
class PeerRow:
    """One peer's forward consensus snapshot."""
    ticker: str
    current_price: float | None = None
    # Forward revenue growth (current_year consensus YoY)
    fwd_rev_growth_pct: float | None = None
    # Forward EPS growth (current_year consensus YoY)
    fwd_eps_growth_pct: float | None = None
    # EPS forward estimate itself
    fwd_eps: float | None = None
    # P/E derived from current_price / fwd_eps
    fwd_pe: float | None = None
    # Up / down revisions over 30 days (signal of momentum)
    up_revs_30d: int = 0
    down_revs_30d: int = 0
    # Revision delta in EPS
    eps_30d_delta: float | None = None
    # Valuation multiples (from yfinance .info; enrichment is opt-in)
    trailing_pe: float | None = None
    ev_ebitda: float | None = None
    ebitda_growth_pct: float | None = None
    # Raw error string if fetch failed
    error: str = ""


@dataclass
class PeerComps:
    """Bundle of peer rows plus context."""
    subject_ticker: str
    schema_type: str
    peer_tickers_attempted: list = field(default_factory=list)
    rows: list = field(default_factory=list)                  # list[PeerRow]
    fetched_at: str = ""

    def to_dict(self) -> dict:
        return {
            "subject_ticker": self.subject_ticker,
            "schema_type": self.schema_type,
            "peer_tickers_attempted": self.peer_tickers_attempted,
            "rows": [asdict(r) if hasattr(r, "__dataclass_fields__") else r
                     for r in self.rows],
            "fetched_at": self.fetched_at,
        }

    def to_prompt_text(self) -> str:
        """
        Compact per-peer table for injection into the brief prompt.
        Example output:
            === PEER CONSENSUS TABLE ===
            Peer   FY Rev %   FY EPS %   Fwd P/E   Revs (30d)
            CMG    +14.2%     +12.5%     45.0×     ↑12 / ↓3
            TXRH   +11.5%     +9.8%      35.2×     ↑8  / ↓4
            ...
            ===================================
        """
        if not self.rows:
            return ""
        lines = [
            f"=== PEER CONSENSUS — {self.subject_ticker} vs. {self.schema_type} peers ===",
            "(Real forward consensus from yfinance — cite specific figures when used)",
        ]
        header = f"{'Peer':<6} {'FY Rev %':>10} {'FY EPS %':>10} {'Fwd P/E':>9} {'Revs 30d':>12}"
        lines.append(header)
        lines.append("-" * len(header))
        for r in self.rows:
            rev = f"{r.fwd_rev_growth_pct:+.1f}%" if r.fwd_rev_growth_pct is not None else "   —"
            eps = f"{r.fwd_eps_growth_pct:+.1f}%" if r.fwd_eps_growth_pct is not None else "   —"
            pe = f"{r.fwd_pe:.1f}×" if r.fwd_pe is not None else "    —"
            revs_bit = ""
            if r.up_revs_30d or r.down_revs_30d:
                revs_bit = f"↑{r.up_revs_30d} / ↓{r.down_revs_30d}"
            else:
                revs_bit = "—"
            lines.append(f"{r.ticker:<6} {rev:>10} {eps:>10} {pe:>9} {revs_bit:>12}")
        if any(r.error for r in self.rows):
            errs = [f"{r.ticker}: {r.error}" for r in self.rows if r.error]
            lines.append(f"Note — fetch errors: {'; '.join(errs[:3])}")
        lines.append(f"Fetched: {self.fetched_at}")
        lines.append("=" * 60)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Fetcher
# --------------------------------------------------------------------------

_CACHE_DIR = Path("data/peer_cache")
_CACHE_TTL_SECONDS = 24 * 60 * 60


def _cache_path(ticker: str, schema: str) -> Path:
    return _CACHE_DIR / f"{ticker.upper()}_{schema.lower()}.json"


def _load_cache(ticker: str, schema: str) -> PeerComps | None:
    p = _cache_path(ticker, schema)
    if not p.exists():
        return None
    age = time.time() - p.stat().st_mtime
    if age > _CACHE_TTL_SECONDS:
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        pc = PeerComps(
            subject_ticker=d.get("subject_ticker", ticker),
            schema_type=d.get("schema_type", schema),
            peer_tickers_attempted=d.get("peer_tickers_attempted", []),
            fetched_at=d.get("fetched_at", ""),
        )
        for rd in d.get("rows", []):
            known = {f for f in PeerRow.__dataclass_fields__}
            pc.rows.append(PeerRow(**{k: v for k, v in rd.items() if k in known}))
        return pc
    except Exception:
        return None


def _save_cache(pc: PeerComps) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(pc.subject_ticker, pc.schema_type)
    path.write_text(json.dumps(pc.to_dict(), default=str, indent=2),
                     encoding="utf-8")


def _row_from_consensus(ticker: str, cd) -> PeerRow:
    """Derive a PeerRow from a ConsensusData object."""
    row = PeerRow(ticker=ticker)
    if cd is None:
        row.error = "no consensus data"
        return row
    if getattr(cd, "error", ""):
        row.error = cd.error
        return row

    # Forward year stats from current_year
    cy = getattr(cd, "current_year", None)
    if cy:
        # Protect against both dataclass and dict shape
        def g(obj, attr, default=None):
            if obj is None:
                return default
            if hasattr(obj, attr):
                return getattr(obj, attr)
            if isinstance(obj, dict):
                return obj.get(attr, default)
            return default
        row.fwd_eps = g(cy, "eps_mean")
        row.fwd_eps_growth_pct = (
            g(cy, "eps_growth_yoy") * 100 if g(cy, "eps_growth_yoy") is not None else None
        )
        row.fwd_rev_growth_pct = (
            g(cy, "revenue_growth_yoy") * 100 if g(cy, "revenue_growth_yoy") is not None else None
        )
        # Revisions
        row.up_revs_30d = int(g(cy, "up_revs_30d", 0) or 0)
        row.down_revs_30d = int(g(cy, "down_revs_30d", 0) or 0)
        cur = g(cy, "eps_current")
        d30 = g(cy, "eps_30d_ago")
        if cur is not None and d30 is not None:
            row.eps_30d_delta = cur - d30

    # Current price
    pt = getattr(cd, "price_target", None)
    if pt is not None:
        if hasattr(pt, "current_price"):
            row.current_price = pt.current_price
        elif isinstance(pt, dict):
            row.current_price = pt.get("current_price")

    # Forward P/E
    if row.current_price and row.fwd_eps and row.fwd_eps > 0:
        row.fwd_pe = row.current_price / row.fwd_eps

    return row


def _fetch_multiples(ticker: str) -> dict:
    """Pull valuation multiples from yfinance .info (EV/EBITDA, trailing P/E)
    plus a best-effort EBITDA growth from the annual income statement. All
    network, all wrapped — any failure just leaves the field None."""
    out = {"trailing_pe": None, "ev_ebitda": None, "ebitda_growth_pct": None}
    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)
        info = tk.info or {}
        out["trailing_pe"] = info.get("trailingPE")
        out["ev_ebitda"] = info.get("enterpriseToEbitda")
        try:
            fin = tk.income_stmt
            if fin is not None and "EBITDA" in list(fin.index):
                vals = [v for v in fin.loc["EBITDA"].tolist() if v is not None]
                if len(vals) >= 2 and vals[1]:
                    out["ebitda_growth_pct"] = (vals[0] - vals[1]) / abs(vals[1]) * 100
        except Exception:
            pass
    except Exception:
        pass
    return out


def build_peer_comps(subject_ticker: str, peer_list, schema_label: str = "curated",
                     include_subject: bool = True, verbose: bool = False) -> PeerComps:
    """Build a PeerComps from an EXPLICIT peer list, bypassing the schema
    registry, and enrich every row with EV/EBITDA + trailing P/E + EBITDA
    growth. Use this to assemble correct, hand-curated comp sets (e.g. COST
    vs WMT/TGT/BJ/DG/KR) instead of whatever schema the picker guessed."""
    from research.consensus_loader import fetch_consensus
    tickers = ([subject_ticker.upper()] if include_subject else []) + [p.upper() for p in peer_list]
    pc = PeerComps(subject_ticker=subject_ticker.upper(), schema_type=schema_label,
                   peer_tickers_attempted=tickers,
                   fetched_at=datetime.now().isoformat(timespec="seconds"))
    for p in tickers:
        if verbose:
            print(f"  build_peer_comps: {p}...")
        try:
            cd = fetch_consensus(p, verbose=False)
        except Exception:
            cd = None
        row = _row_from_consensus(p, cd)
        m = _fetch_multiples(p)
        row.trailing_pe = m.get("trailing_pe")
        row.ev_ebitda = m.get("ev_ebitda")
        row.ebitda_growth_pct = m.get("ebitda_growth_pct")
        pc.rows.append(row)
        time.sleep(0.3)
    return pc


def fetch_peer_comps(
    subject_ticker: str,
    schema_type: str,
    max_peers: int = 4,
    verbose: bool = False,
    force_refresh: bool = False,
) -> PeerComps:
    """
    Fetch forward consensus for the schema's peer group (excluding the
    subject). Caches per-subject-per-day.
    """
    if not force_refresh:
        cached = _load_cache(subject_ticker, schema_type)
        if cached:
            if verbose:
                print(f"  Peer comps: cache hit ({len(cached.rows)} peers, "
                      f"fetched {cached.fetched_at})")
            return cached

    peers = peers_for(schema_type, exclude=subject_ticker, max_peers=max_peers)
    pc = PeerComps(
        subject_ticker=subject_ticker.upper(),
        schema_type=schema_type,
        peer_tickers_attempted=list(peers),
        fetched_at=datetime.now().isoformat(timespec="seconds"),
    )
    if not peers:
        if verbose:
            print(f"  Peer comps: no peers registered for schema '{schema_type}'")
        return pc

    # Reuse the existing consensus loader — it already handles yfinance
    # quirks, retries, and produces the right dataclass shape.
    try:
        from research.consensus_loader import fetch_consensus
    except Exception as e:
        if verbose:
            print(f"  Peer comps: can't import fetch_consensus: {e}")
        return pc

    for p in peers:
        if verbose:
            print(f"  Peer comps: fetching {p}...")
        try:
            cd = fetch_consensus(p, verbose=False)
        except Exception as e:
            cd = None
            if verbose:
                print(f"    {p}: {type(e).__name__}: {e}")
        row = _row_from_consensus(p, cd)
        pc.rows.append(row)
        # Rate-limit politely
        time.sleep(0.4)

    try:
        _save_cache(pc)
    except Exception as e:
        if verbose:
            print(f"  Peer comps: cache save failed: {e}")

    if verbose:
        n_ok = sum(1 for r in pc.rows if not r.error)
        print(f"  Peer comps: {n_ok}/{len(pc.rows)} peers loaded")
    return pc


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main():
    import argparse
    p = argparse.ArgumentParser(description="Fetch peer comps")
    p.add_argument("ticker")
    p.add_argument("schema", help="Schema key: restaurant, franchise_restaurant, software, ...")
    p.add_argument("--peers", type=int, default=4)
    p.add_argument("--refresh", action="store_true")
    args = p.parse_args()

    pc = fetch_peer_comps(
        args.ticker, args.schema,
        max_peers=args.peers, verbose=True, force_refresh=args.refresh,
    )
    print()
    print(pc.to_prompt_text())


if __name__ == "__main__":
    _main()
