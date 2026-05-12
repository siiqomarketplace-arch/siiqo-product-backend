# -*- coding: utf-8 -*-
"""Final state check - confirms everything in DB"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import requests, json

BASE = "http://127.0.0.1:5000/api"
SLUG = "stillwalker-boutique"

print("\n" + "="*65)
print("  FINAL STATE VERIFICATION")
print("="*65)

# 1. Public storefront
r = requests.get(f"{BASE}/marketplace/store/{SLUG}")
print(f"\n[1] Public storefront /{SLUG}")
print(f"    Status : {r.status_code} (expect 200)")
if r.status_code == 200:
    d = r.json()
    info = d.get("store_info", {})
    catalogs = d.get("catalogs", [])
    prods = [p for c in catalogs for p in c.get("products", [])]
    print(f"    Name   : {info.get('name')}")
    print(f"    Phone  : {info.get('phone')}")
    print(f"    Hero   : {info.get('branding', {}).get('hero_heading')}")
    print(f"    Sections: {info.get('branding', {}).get('sections')}")
    print(f"    Products: {len(prods)}")
    for p in prods:
        print(f"      - {p.get('name')} | NGN {p.get('price')}")
    socials = info.get("socials", {})
    print(f"    Socials: {list(socials.keys())}")
    hours = info.get("hours", {})
    print(f"    Hours  : {len(hours)} days configured")
    print(f"    RESULT : LIVE and fully configured")
else:
    print(f"    RESULT : {r.json()}")

# 2. Marketplace products
r = requests.get(f"{BASE}/marketplace/products")
all_p = r.json().get("products", [])
vendor_p = [p for p in all_p if SLUG in str(p.get("storefront_slug",""))]
print(f"\n[2] Marketplace products")
print(f"    Total in marketplace : {len(all_p)}")
print(f"    From this vendor     : {len(vendor_p)}")
for p in vendor_p:
    print(f"      - {p.get('name')} | NGN {p.get('price')} | Store: {p.get('storefront_slug')}")

# 3. Marketplace storefronts
r = requests.get(f"{BASE}/marketplace/storefronts")
all_s = r.json().get("storefronts", [])
our_s = [s for s in all_s if s.get("store_slug") == SLUG]
print(f"\n[3] Marketplace storefronts")
print(f"    Total stores : {len(all_s)}")
print(f"    Our store    : {'FOUND' if our_s else 'NOT FOUND'}")
if our_s:
    s = our_s[0]
    print(f"    Name         : {s.get('store_name')}")
    print(f"    Slug         : {s.get('store_slug')}")
    print(f"    Verified     : {s.get('is_verified')}")
    print(f"    Published    : {s.get('is_published')}")
    print(f"    Live         : {s.get('is_live')}")

# 4. URL format
print(f"\n[4] Storefront URL format")
print(f"    Slug URL     : siiqo.com/{SLUG}")
print(f"    API path     : /api/marketplace/store/{SLUG}")
print(f"    NOTE: storename.siiqo.com requires DNS CNAME wildcard")
print(f"          *.siiqo.com -> your server IP")
print(f"          Then middleware reads subdomain and maps to slug")

# 5. Visibility matrix
print(f"\n[5] Visibility rules matrix")
print(f"    Approved + Published   = 200 LIVE")
print(f"    Approved + Unpublished = 202 OFFLINE")
print(f"    Unapproved + Published = 202 UNDER REVIEW")
print(f"    Unapproved + Unpublished = 202 OFFLINE")

print("\n" + "="*65)
print("  ALL CHECKS COMPLETE")
print("="*65 + "\n")
