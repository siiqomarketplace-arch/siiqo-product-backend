"""Deterministic name matching (never AI). Used for NIN name ↔ payout account
name, and verified person ↔ CAC director."""
import re

# Words that carry no identity information in Nigerian names / account names.
_NOISE = {"mr", "mrs", "miss", "ms", "dr", "chief", "alhaji", "alhaja", "engr", "prof",
          "ltd", "limited", "enterprise", "enterprises", "ventures", "global", "nig", "nigeria"}


def name_tokens(name: str) -> set[str]:
    words = re.findall(r"[a-z]+", (name or "").lower())
    return {w for w in words if len(w) > 1 and w not in _NOISE}


def names_match(a: str, b: str) -> bool:
    """True when two names plausibly refer to the same person.

    Order, middle names, titles and case are ignored. At least two name words
    must match, or every word of a one-word name must match.
    """
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return False
    common = ta & tb
    if len(common) >= 2:
        return True
    return len(common) == 1 and min(len(ta), len(tb)) == 1
