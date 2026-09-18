"""
intent_parser.py — Siiqo 2.0 Lean Intent Extractor
Extracts structured buyer intent (Category, Location, Budget, Entity Type)
from natural language queries without expensive or slow external LLM calls.
"""
import re
import unicodedata

# Common Nigerian commercial hubs and major cities
NIGERIAN_CITIES = {
    "abuja": "Abuja",
    "fct": "Abuja",
    "lagos": "Lagos",
    "ikeja": "Lagos",
    "lekki": "Lagos",
    "yaba": "Lagos",
    "victoria island": "Lagos",
    "vi": "Lagos",
    "surulere": "Lagos",
    "ibadan": "Ibadan",
    "port harcourt": "Port Harcourt",
    "ph": "Port Harcourt",
    "kano": "Kano",
    "enugu": "Enugu",
    "benin": "Benin City",
    "benin city": "Benin City",
    "kaduna": "Kaduna",
    "calabar": "Calabar",
    "owerri": "Owerri",
    "warri": "Warri",
    "jos": "Jos",
    "abeokuta": "Abeokuta",
    "ilorin": "Ilorin",
    "asaba": "Asaba",
    "akure": "Akure",
    "uyo": "Uyo",
}

# Category keywords mapping
CATEGORY_KEYWORDS = {
    "Food & Drinks": [
        "cake", "cakes", "bakery", "baker", "pastry", "pastries", "catering", "caterer",
        "food", "soup", "jollof", "shawarma", "snacks", "drinks", "smoothie", "chops", "small chops"
    ],
    "Fashion": [
        "clothes", "clothing", "dress", "dresses", "gown", "gowns", "shoes", "shoe", "sneakers",
        "heels", "ankara", "fabric", "fabrics", "tailor", "designer", "suit", "shirt", "pants",
        "trousers", "wig", "wigs", "hair", "perfume", "fragrance", "jewelry", "bag", "bags"
    ],
    "Electronics": [
        "phone", "phones", "iphone", "samsung", "laptop", "laptops", "macbook", "computer",
        "airpods", "headphones", "charger", "gadget", "gadgets", "tv", "camera", "tablet", "ipad"
    ],
    "Beauty": [
        "skincare", "makeup", "lashes", "cream", "lotion", "lipstick", "cosmetics", "facial",
        "haircut", "barber", "nails", "spa"
    ],
    "Services": [
        "freelance", "freelancer", "developer", "designer", "graphic designer", "branding",
        "consultant", "photographer", "photography", "videographer", "electrician", "plumber",
        "cleaning", "mechanic", "carpenter", "repair"
    ],
    "Home & Furniture": [
        "furniture", "chair", "chairs", "table", "bed", "sofa", "couch", "decor", "interior",
        "curtain", "mattress", "kitchen", "cookware"
    ],
}

# Vendor/Business intent keywords
VENDOR_INTENT_TERMS = [
    "vendor", "vendors", "store", "stores", "shop", "shops", "seller", "sellers",
    "baker", "bakers", "tailor", "tailors", "designer", "designers", "company", "business"
]


def parse_buyer_intent(query_str: str) -> dict:
    """
    Parses natural language search queries into structured attributes.
    Example: "birthday cake in Abuja under 40k"
    Returns:
    {
        "raw_query": "birthday cake in Abuja under 40k",
        "keyword": "birthday cake",
        "city": "Abuja",
        "max_price": 40000.0,
        "min_price": None,
        "category_hint": "Food & Drinks",
        "entity_preference": "vendor",
        "has_intent": True
    }
    """
    if not query_str or not isinstance(query_str, str):
        return {
            "raw_query": "",
            "keyword": "",
            "city": None,
            "max_price": None,
            "min_price": None,
            "category_hint": None,
            "entity_preference": "all",
            "has_intent": False,
        }

    raw = query_str.strip()
    clean = unicodedata.normalize("NFKD", raw)
    text = f" {clean.lower()} "

    extracted_city = None
    max_price = None
    min_price = None
    category_hint = None
    entity_preference = "all"

    # 1. Extract Price Constraints (e.g. "under 40k", "under 40,000", "<50k", "below 30k")
    # Matches: under/below/less than 40k or 40000 or 40,000 or ₦40,000
    max_price_match = re.search(
        r'(?:under|below|less\s+than|within|<=?|max(?:imum)?)\s*[:=]?\s*(?:ngn|n|₦)?\s*([0-9]+(?:,[0-9]{3})*|\d+)\s*(k|m)?\b',
        text,
        re.IGNORECASE
    )
    if max_price_match:
        val_str = max_price_match.group(1).replace(",", "")
        multiplier = 1000 if (max_price_match.group(2) or "").lower() == "k" else (1000000 if (max_price_match.group(2) or "").lower() == "m" else 1)
        try:
            max_price = float(val_str) * multiplier
            text = text.replace(max_price_match.group(0), " ")
        except (ValueError, TypeError):
            pass

    # Simple suffix price: "40k" at the end or preceded by "for 40k"
    if max_price is None:
        suffix_price_match = re.search(r'\b(?:for|at)?\s*(?:ngn|n|₦)?\s*(\d+)\s*k\b', text, re.IGNORECASE)
        if suffix_price_match:
            try:
                max_price = float(suffix_price_match.group(1)) * 1000
                text = text.replace(suffix_price_match.group(0), " ")
            except (ValueError, TypeError):
                pass

    # 2. Extract City/Location
    # Match phrases like "in abuja", "around lagos", "at ikeja" or standalone city names
    for city_key, city_proper in sorted(NIGERIAN_CITIES.items(), key=lambda x: len(x[0]), reverse=True):
        pattern = rf'\b(?:in|at|around|near|within)?\s*{re.escape(city_key)}\b'
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            extracted_city = city_proper
            text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
            break

    # 3. Detect Entity Preference (vendor/store vs product)
    for v_term in VENDOR_INTENT_TERMS:
        pattern = rf'\b{re.escape(v_term)}\b'
        if re.search(pattern, text, re.IGNORECASE):
            entity_preference = "vendor"
            text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
            break

    # 4. Clean and identify Category
    remaining_clean = " ".join(text.split())
    # Remove noise stop words
    stop_words = {"i", "want", "need", "looking", "for", "find", "get", "me", "a", "an", "the", "some", "good", "best", "affordable", "cheap"}
    tokens = [t for t in remaining_clean.split() if t not in stop_words]

    # Detect category hint based on remaining tokens and raw query
    for cat_name, keywords in CATEGORY_KEYWORDS.items():
        if any(re.search(rf'\b{re.escape(kw)}\b', raw.lower()) for kw in keywords):
            category_hint = cat_name
            break

    core_keyword = " ".join(tokens).strip()
    if not core_keyword and raw:
        # Fallback to stripped raw if everything got filtered
        core_keyword = raw

    has_structured_intent = bool(extracted_city or max_price or (category_hint and entity_preference != "all"))

    return {
        "raw_query": raw,
        "keyword": core_keyword,
        "city": extracted_city,
        "max_price": max_price,
        "min_price": min_price,
        "category_hint": category_hint,
        "entity_preference": entity_preference,
        "has_intent": has_structured_intent,
    }
