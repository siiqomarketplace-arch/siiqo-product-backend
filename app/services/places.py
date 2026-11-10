"""Nigerian places: turn the free-text place a seller types ("Wuse 2, Abuja",
"Ikeja Lagos", "Enugu State") into a (city, state) pair.

Used by store setup (the /start interview asks one "location" question) and
the boot-time backfill of stores that were published without a city/state.
Never guesses: text with no known state or well-known city keeps the text as
the city and leaves the state empty.
"""
from __future__ import annotations

import re

# The 36 states and the FCT, as stored on Storefront.state.
NIGERIAN_STATES = (
    "Abia", "Adamawa", "Akwa Ibom", "Anambra", "Bauchi", "Bayelsa", "Benue",
    "Borno", "Cross River", "Delta", "Ebonyi", "Edo", "Ekiti", "Enugu", "Gombe",
    "Imo", "Jigawa", "Kaduna", "Kano", "Katsina", "Kebbi", "Kogi", "Kwara",
    "Lagos", "Nasarawa", "Niger", "Ogun", "Ondo", "Osun", "Oyo", "Plateau",
    "Rivers", "Sokoto", "Taraba", "Yobe", "Zamfara", "FCT",
)

# Alias (normalised tokens) -> state.
_STATE_ALIASES: dict[tuple[str, ...], str] = {}
for _s in NIGERIAN_STATES:
    _STATE_ALIASES[tuple(_s.lower().split())] = _s
_STATE_ALIASES.update({
    ("akwaibom",): "Akwa Ibom",
    ("crossriver",): "Cross River",
    ("nassarawa",): "Nasarawa",
    ("abuja",): "FCT",
    ("f", "c", "t"): "FCT",
    ("federal", "capital", "territory"): "FCT",
})

# Well-known cities whose state is not in doubt, for text with no state in it.
_CITY_STATE: dict[tuple[str, ...], tuple[str, str]] = {
    ("ikeja",): ("Ikeja", "Lagos"),
    ("lekki",): ("Lekki", "Lagos"),
    ("yaba",): ("Yaba", "Lagos"),
    ("surulere",): ("Surulere", "Lagos"),
    ("ajah",): ("Ajah", "Lagos"),
    ("ikorodu",): ("Ikorodu", "Lagos"),
    ("victoria", "island"): ("Victoria Island", "Lagos"),
    ("ibadan",): ("Ibadan", "Oyo"),
    ("port", "harcourt"): ("Port Harcourt", "Rivers"),
    ("benin", "city"): ("Benin City", "Edo"),
    ("calabar",): ("Calabar", "Cross River"),
    ("uyo",): ("Uyo", "Akwa Ibom"),
    ("owerri",): ("Owerri", "Imo"),
    ("warri",): ("Warri", "Delta"),
    ("asaba",): ("Asaba", "Delta"),
    ("jos",): ("Jos", "Plateau"),
    ("abeokuta",): ("Abeokuta", "Ogun"),
    ("ilorin",): ("Ilorin", "Kwara"),
    ("onitsha",): ("Onitsha", "Anambra"),
    ("awka",): ("Awka", "Anambra"),
    ("nnewi",): ("Nnewi", "Anambra"),
    ("aba",): ("Aba", "Abia"),
    ("umuahia",): ("Umuahia", "Abia"),
    ("akure",): ("Akure", "Ondo"),
    ("osogbo",): ("Osogbo", "Osun"),
    ("ile", "ife"): ("Ile-Ife", "Osun"),
    ("zaria",): ("Zaria", "Kaduna"),
    ("maiduguri",): ("Maiduguri", "Borno"),
    ("yola",): ("Yola", "Adamawa"),
    ("makurdi",): ("Makurdi", "Benue"),
    ("lokoja",): ("Lokoja", "Kogi"),
    ("minna",): ("Minna", "Niger"),
    ("abakaliki",): ("Abakaliki", "Ebonyi"),
    ("yenagoa",): ("Yenagoa", "Bayelsa"),
    ("ado", "ekiti"): ("Ado-Ekiti", "Ekiti"),
    ("lafia",): ("Lafia", "Nasarawa"),
}

_MAX_CITY = 100
_DROP = {"state", "nigeria", "ng"}


def _tokens(text: str) -> list[tuple[str, str]]:
    """(original, normalised) word pairs; commas and dashes split words."""
    out = []
    for raw in re.split(r"[\s,;/|\-]+", text):
        raw = raw.strip(" .()'\"")
        if not raw:
            continue
        norm = re.sub(r"[^a-z0-9]", "", raw.lower())
        if norm:
            out.append((raw, norm))
    return out


def _last_match(norms: list[str], table: dict) -> tuple[int, int, object] | None:
    """The match that ends last (then the longest) of any alias in ``table``."""
    best = None
    for alias, value in table.items():
        n = len(alias)
        for i in range(len(norms) - n + 1):
            if tuple(norms[i:i + n]) == alias:
                key = (i + n, n)
                if best is None or key > best[0]:
                    best = (key, i, value)
    if best is None:
        return None
    (end, n), start, value = best
    return start, end, value


def _clip(text: str | None) -> str | None:
    text = (text or "").strip(" ,.-")
    return text[:_MAX_CITY].strip() or None


def split_place(text) -> tuple[str | None, str | None]:
    """Free-text place -> (city, state). Either may be None."""
    if not isinstance(text, str) or not text.strip():
        return None, None
    text = " ".join(text.split())
    pairs = [p for p in _tokens(text) if p[1] not in _DROP]
    norms = [n for _, n in pairs]

    hit = _last_match(norms, _STATE_ALIASES)
    if hit:
        start, end, state = hit
        if state == "FCT":
            return "Abuja", "FCT"
        rest = [o for o, _ in pairs[:start]] + [o for o, _ in pairs[end:]]
        rest_norm = [n for _, n in pairs[:start]] + [n for _, n in pairs[end:]]
        # "Benin City, Edo" -> the known city's own spelling.
        city_hit = _last_match(rest_norm, _CITY_STATE)
        if city_hit and city_hit[2][1] == state:
            return city_hit[2][0], state
        return _clip(" ".join(rest)), state

    city_hit = _last_match(norms, _CITY_STATE)
    if city_hit:
        return city_hit[2]
    return _clip(text), None


def clean_place(city, state) -> tuple[str | None, str | None]:
    """A store's saved city/state, tidied for display and matching.

    Sellers saved the same place many ways ("FCT, Nigeria", "Abuja, Nigeria",
    "Federal Capital Territory, Nigeria", "Lagos state"). This reads them as
    one state without rewriting what is stored. Text that names no Nigerian
    state is kept, minus a trailing "Nigeria"; nothing is guessed.
    """
    def _strip(v):
        if not isinstance(v, str):
            return None
        words = [o for o, n in _tokens(v) if n not in ("nigeria", "ng")]
        return _clip(" ".join(words))

    raw_city, raw_state = _strip(city), _strip(state)
    canon = None
    if raw_state:
        hit = _last_match([n for _, n in _tokens(raw_state) if n not in _DROP], _STATE_ALIASES)
        canon = hit[2] if hit else None
    if canon is None and raw_city:
        # No usable state: the city text may hold one ("Ikeja Lagos", "Abuja").
        c2, s2 = split_place(raw_city)
        if s2:
            raw_city, canon = c2, s2
    out_state = canon or raw_state
    out_city = raw_city
    if out_city and out_state and out_city.strip().lower() == out_state.strip().lower():
        out_city = None                                  # "Lagos, Lagos" -> "Lagos"
    if canon == "FCT" and (not out_city or out_city.lower() in ("fct", "federal capital territory")):
        out_city = "Abuja"
    return out_city, out_state


def place_terms(query) -> list[str]:
    """Search terms for a place filter. A state name (in any spelling) expands to
    every way sellers wrote it, so "FCT" also finds "Abuja, Nigeria" and
    "Federal Capital Territory". Anything else is used as typed."""
    q = " ".join(str(query or "").split())
    if not q:
        return []
    norms = [n for _, n in _tokens(q) if n not in _DROP]
    hit = _last_match(norms, _STATE_ALIASES)
    if not hit or (hit[1] - hit[0]) != len(norms):
        return [q]                                       # a city or free text, not just a state
    state = hit[2]
    terms = {state}
    for alias, value in _STATE_ALIASES.items():
        if value == state:
            terms.add("".join(alias) if all(len(a) == 1 for a in alias) else " ".join(alias))
    return sorted(terms, key=lambda t: (-len(t), t))


def place_label(city, state) -> str | None:
    """'City, State' (or whichever part exists) for product.location."""
    parts = []
    for p in (city, state):
        p = (p or "").strip()
        if p and p.lower() not in [x.lower() for x in parts]:
            parts.append(p)
    return ", ".join(parts) or None


def backfill_store_places() -> dict:
    """Fill missing store city/state from the seller's store-setup answers, then
    missing physical product locations from their store. Idempotent: only empty
    values are filled, never overwritten. Cheap when there is nothing to do."""
    from sqlalchemy import or_

    from app.extensions import db
    from app.models.onboarding import OnboardingSession
    from app.models.product import Product
    from app.models.user import Storefront

    stats = {"stores": 0, "products": 0}

    def _blank(col):
        return or_(col.is_(None), col == "")

    missing = (db.session.query(Storefront.id, Storefront.vendor_id)
               .filter(_blank(Storefront.city), _blank(Storefront.state)).all())
    if missing:
        by_vendor = {vendor_id: store_id for store_id, vendor_id in missing}
        sessions = (OnboardingSession.query
                    .filter(OnboardingSession.user_id.in_(list(by_vendor)))
                    .order_by(OnboardingSession.created_at.desc(), OnboardingSession.id.desc())
                    .all())
        done = set()
        for s in sessions:
            if s.user_id in done:
                continue
            answers = s.answers or {}
            city, state = answers.get("city"), answers.get("state")
            if not city and not state:
                city, state = split_place(answers.get("location"))
            if not city and not state:
                continue  # an older session may still hold the place
            done.add(s.user_id)
            stats["stores"] += (db.session.query(Storefront)
                                .filter(Storefront.id == by_vendor[s.user_id],
                                        _blank(Storefront.city), _blank(Storefront.state))
                                .update({"city": (city or None) and str(city)[:100],
                                         "state": (state or None) and str(state)[:100]},
                                        synchronize_session=False))

    rows = (db.session.query(Product.storefront_id, Storefront.city, Storefront.state)
            .join(Storefront, Storefront.id == Product.storefront_id)
            .filter(_blank(Product.location), Product.product_type == "physical",
                    or_(Storefront.city.isnot(None), Storefront.state.isnot(None)))
            .distinct().all())
    for store_id, city, state in rows:
        label = place_label(city, state)
        if not label:
            continue
        stats["products"] += (db.session.query(Product)
                              .filter(Product.storefront_id == store_id, _blank(Product.location),
                                      Product.product_type == "physical")
                              .update({"location": label[:255]}, synchronize_session=False))
    if stats["stores"] or stats["products"]:
        db.session.commit()
    else:
        db.session.rollback()
    return stats
