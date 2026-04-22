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

    # ── Healthcare insurance / managed care ──
    "healthcare_insurance": [
        "UNH", "ELV", "HUM", "CI", "CVS", "CNC", "MOH",
    ],

    # ── Pharma / biotech large-cap ──
    "pharma": [
        "LLY", "NVO", "JNJ", "PFE", "MRK", "ABBV", "BMY", "AMGN", "GILD",
    ],

    # ── Internet / platforms ──
    "internet": [
        "META", "GOOG", "GOOGL", "NFLX", "SNAP", "PINS", "RDDT", "SPOT",
    ],

    # ── Consumer electronics / hardware ──
    "consumer_electronics": [
        "AAPL", "SONY", "LOGI", "GRMN",
    ],

    # ── Payments / fintech ──
    "payments": [
        "V", "MA", "PYPL", "FI", "SQ", "AXP", "COF",
    ],

    # ── Banks (large-cap US) ──
    "banks": [
        "JPM", "BAC", "WFC", "C", "GS", "MS", "USB", "PNC",
    ],

    # ── Energy / integrated oil + gas ──
    "energy": [
        "XOM", "CVX", "COP", "OXY", "EOG", "SLB", "PSX", "MPC",
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


# --------------------------------------------------------------------------
# Schema inference from yfinance sector/industry
# --------------------------------------------------------------------------

# yfinance industry strings → our peer schema key. More specific than
# sector — use this first.
_YF_INDUSTRY_TO_SCHEMA = {
    # Restaurants / franchise
    "Restaurants": "restaurant",
    # Software
    "Software - Application":     "software",
    "Software - Infrastructure":  "software",
    "Information Technology Services": "software",
    # Semis
    "Semiconductors":                       "semiconductors",
    "Semiconductor Equipment & Materials":  "semiconductors",
    # Med devices
    "Medical Devices":         "med_devices",
    "Medical Instruments & Supplies": "med_devices",
    "Diagnostics & Research":  "med_devices",
    # Healthcare insurance / managed care — distinct from devices
    "Healthcare Plans":        "healthcare_insurance",
    # Pharma / biotech
    "Drug Manufacturers - General":       "pharma",
    "Drug Manufacturers - Specialty & Generic": "pharma",
    "Biotechnology":                      "pharma",
    # Internet / platforms
    "Internet Content & Information":     "internet",
    "Entertainment":                      "internet",
    # Consumer electronics / hardware
    "Consumer Electronics":               "consumer_electronics",
    "Communication Equipment":            "consumer_electronics",
    # Payments / fintech
    "Credit Services":                    "payments",
    # Banks
    "Banks - Diversified":                "banks",
    "Banks - Regional":                   "banks",
    # Energy
    "Oil & Gas Integrated":               "energy",
    "Oil & Gas E&P":                      "energy",
    "Oil & Gas Equipment & Services":     "energy",
    "Oil & Gas Refining & Marketing":     "energy",
    "Oil & Gas Midstream":                "energy",
    # Consumer staples
    "Packaged Foods":  "consumer_staples",
    "Beverages - Non-Alcoholic": "consumer_staples",
    "Beverages - Wineries & Distilleries": "consumer_staples",
    "Tobacco":         "consumer_staples",
    "Household & Personal Products": "consumer_staples",
    "Confectioners":   "consumer_staples",
    "Food Distribution": "consumer_staples",
    "Discount Stores": "consumer_staples",
    "Grocery Stores":  "consumer_staples",
    # Consumer discretionary / retail / apparel
    "Specialty Retail":               "consumer_discretionary",
    "Apparel Retail":                 "consumer_discretionary",
    "Apparel Manufacturing":          "consumer_discretionary",
    "Footwear & Accessories":         "consumer_discretionary",
    "Home Improvement Retail":        "consumer_discretionary",
    "Luxury Goods":                   "consumer_discretionary",
    "Leisure":                        "consumer_discretionary",
    "Travel Services":                "consumer_discretionary",
    "Lodging":                        "consumer_discretionary",
    "Department Stores":              "consumer_discretionary",
    "Auto Manufacturers":             "consumer_discretionary",
    "Auto & Truck Dealerships":       "consumer_discretionary",
    "Gambling":                       "consumer_discretionary",
    "Resorts & Casinos":              "consumer_discretionary",
    # Industrials
    "Specialty Industrial Machinery":       "industrials",
    "Farm & Heavy Construction Machinery":  "industrials",
    "Aerospace & Defense":                  "industrials",
    "Electrical Equipment & Parts":         "industrials",
    "Industrial Distribution":              "industrials",
    "Engineering & Construction":           "industrials",
    "Integrated Freight & Logistics":       "industrials",
    "Railroads":                            "industrials",
    "Airlines":                             "industrials",
    "Tools & Accessories":                  "industrials",
}

# yfinance sector strings → our peer schema key. Broader fallback.
_YF_SECTOR_TO_SCHEMA = {
    "Consumer Cyclical":   "consumer_discretionary",
    "Consumer Defensive":  "consumer_staples",
    "Technology":          "software",    # coarse; industry check lands semis better
    "Healthcare":          "med_devices",
    "Industrials":         "industrials",
    # Intentionally NO mapping for Financial Services / Real Estate /
    # Energy / Basic Materials / Utilities / Communication Services —
    # we don't have curated peer groups for those yet.
}


def infer_schema_from_yfinance(ticker: str, verbose: bool = False) -> str:
    """
    Ask yfinance for the ticker's sector + industry, then map to one of
    our peer-registry schemas. Returns empty string if we can't map.

    This lets peer-comps fire for any ticker, not just the ~60 curated
    names in PEER_GROUPS. Failures here are silent — the caller decides
    whether to skip or fallback.
    """
    try:
        import yfinance as yf
    except ImportError:
        return ""
    try:
        info = yf.Ticker(ticker.upper()).info or {}
    except Exception as e:
        if verbose:
            print(f"  peer schema inference: yfinance failed for {ticker}: {e}")
        return ""
    industry = (info.get("industry") or "").strip()
    sector = (info.get("sector") or "").strip()

    # Industry is more specific; try first
    if industry and industry in _YF_INDUSTRY_TO_SCHEMA:
        if verbose:
            print(f"  peer schema inference: {ticker} industry='{industry}' "
                  f"-> {_YF_INDUSTRY_TO_SCHEMA[industry]}")
        return _YF_INDUSTRY_TO_SCHEMA[industry]
    # Special-case: franchise restaurants often have 'Restaurants' too
    # (covered above). No additional logic needed.

    if sector and sector in _YF_SECTOR_TO_SCHEMA:
        if verbose:
            print(f"  peer schema inference: {ticker} sector='{sector}' "
                  f"-> {_YF_SECTOR_TO_SCHEMA[sector]}")
        return _YF_SECTOR_TO_SCHEMA[sector]

    if verbose:
        print(f"  peer schema inference: {ticker} sector='{sector}' "
              f"industry='{industry}' — no mapping")
    return ""
