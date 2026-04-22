"""
Peer Registry — maps each sector schema to a curated peer group.

Hand-curated, not auto-generated. The goal is 4-6 high-signal peers per
group: names an analyst would actually look at when pricing a thesis.
Over-large peer lists dilute comparisons; under-size misses the obvious
cross-check (e.g. WING without CMG, TXRH, CAVA in view is incomplete).

Usage:
    from research.peer_registry import peers_for

    peers_for("franchise_restaurant", exclude="WING")
    -> ["CMG", "TXRH", "CAVA", "DPZ", "YUM"]

Peers are returned in a stable order that roughly puts "most comparable"
first so callers can truncate to 3-4 and still hit the highest-signal
names.
"""

from __future__ import annotations


# Curated peer groups. Keys must match the schema_type values used in
# research/deep_research.py (and sector_drivers.py).
#
# Rule of thumb: include names with publicly reported comparable metrics
# (SSS for restaurants, NRR for SaaS) and that analysts actually compare
# to — not random sector peers.
PEER_GROUPS: dict[str, list[str]] = {
    # ── Restaurant (company-operated or mixed) ──
    "restaurant": [
        "CMG", "TXRH", "CAVA", "SG", "DRI", "CAKE", "BLMN",
        "BROS", "JACK", "SHAK", "CBRL",
    ],

    # ── Franchise-heavy restaurant (royalty models) ──
    "franchise_restaurant": [
        "WING", "DPZ", "MCD", "QSR", "YUM", "DRI",
        "DNUT", "PZZA", "TXRH",
    ],

    # ── SaaS / enterprise software ──
    "software": [
        "NOW", "CRM", "DDOG", "SNOW", "CRWD", "TEAM",
        "ZS", "NET", "OKTA", "HUBS", "MDB",
    ],

    # ── Consumer staples / packaged food ──
    "consumer_staples": [
        "KO", "PEP", "KDP", "KHC", "MDLZ", "GIS", "K", "CPB",
    ],

    # ── Consumer discretionary / apparel & specialty retail ──
    "consumer_discretionary": [
        "LULU", "NKE", "TJX", "ROST", "BURL", "GPS", "AEO", "URBN",
    ],

    # ── Semis ──
    "semiconductors": [
        "NVDA", "AMD", "AVGO", "TXN", "MRVL", "ON", "MCHP", "ASML",
    ],

    # ── Industrials / heavy manufacturing ──
    "industrials": [
        "GE", "HON", "ETN", "PH", "ROK", "EMR", "ITW", "DOV",
    ],

    # ── Healthcare / med devices ──
    "med_devices": [
        "MDT", "BSX", "SYK", "ABT", "ISRG", "EW", "BAX", "BDX",
    ],

    # ── Fallback for any sector we haven't curated ──
    "general": [],
}


def peers_for(schema_type: str, exclude: str | None = None,
               max_peers: int = 6) -> list[str]:
    """
    Return the peer tickers for a schema, excluding the subject ticker.

    Tolerates case + whitespace in `schema_type`; falls back to "general"
    (empty list) if the schema isn't registered.
    """
    key = (schema_type or "").strip().lower()
    peers = PEER_GROUPS.get(key, [])
    if not peers:
        return []
    ex = (exclude or "").strip().upper()
    out = [t for t in peers if t.upper() != ex]
    return out[:max_peers]
