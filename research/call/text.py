"""Text hygiene for narrative output: no em dashes, optional provenance tags."""

from __future__ import annotations

import re

EM_DASH = "—"

# Provenance tags used in the full digest. The one-page pitch strips them.
TAGS = {
    "R": "reported fact",
    "G": "management guidance (bias-adjusted where noted)",
    "$": "action backed by money",
    "AI": "management statement against interest",
    "MC": "management claim, unverified",
    "IND": "independent data",
    "EST": "our estimate or inference",
}
_TAG_RE = re.compile(r"\s?\[(?:R|G|\$|AI|MC|IND|EST)(?::[^\]]{1,40})?\]")


def no_em_dash(s: str) -> str:
    """Replace em dashes with plain punctuation."""
    if not isinstance(s, str) or EM_DASH not in s:
        return s
    s = re.sub(r"\s*" + EM_DASH + r"\s*(?=[a-z0-9$(])", ", ", s)
    s = re.sub(r"\s*" + EM_DASH + r"\s*", ": ", s)
    return s


def scrub(obj):
    """Recursively remove em dashes from every string in a JSON-like object."""
    if isinstance(obj, str):
        return no_em_dash(obj)
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    return obj


# Ledger refs in parentheses: (S89), (G45), (S20, S53), (S21 to S79), (IND). Not (AI) or (R&D),
# which are usually real words in a pitch.
_REF = r"(?:[SGM]\d+|IND|EST|MC)"
_REF_RE = re.compile(r"\s?\(" + _REF + r"(?:(?:,\s*|\s+(?:and|to)\s+)" + _REF + r")*\)")


def strip_tags(s: str) -> str:
    return _REF_RE.sub("", _TAG_RE.sub("", s)) if isinstance(s, str) else s
