# -*- coding: utf-8 -*-
import sys, requests, json
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = "http://127.0.0.1:5000/api"
SLUG = "stillwalker-boutique"
PASS, FAIL = 0, 0

def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        print(f"  PASS  {label}" + (f" ({detail})" if detail else ""))
        PASS += 1
    else:
        print(f"  FAIL  {label}" + (f" ({detail})" if detail else ""))
        FAIL += 1

print("\n" + "="*65)
print("  SIIQO FULL FLOW VERIFICATION")
print("="*65)

# 1. Backend up
try:
    r = requests.get(f"http://127.0.0.1:5000/health", timeout=3)
    check("Backend is running", r.status_code == 200, r.json().get("version"))
except:
    print("  FAIL  Backend is NOT running")
    sys.exit(1)

# 2. Public storefront live
r = requests.get(f"{BASE}/marketplace/store/{SLUG}")
check("Public storefront returns 200", r.status_code == 200)
if r.status_code == 200:
    info = r.json().get("store_info", {})
    template = info.get("template_options", {})
    catalogs = r.json().get("catalogs", [])
    prods = [p for c in catalogs for p in c.get("products", [])]
    check("Store name correct", info.get("store_name") == "Stillwalker Boutique", info.get("store_name"))
    check("Hero heading saved", template.get("hero_heading") == "Style That Speaks", template.get("hero_heading"))
    check("Hero CTA saved", template.get("hero_cta") == "Shop Collection", template.get("hero_cta"))
    check("Theme saved", template.get("layout_style") == "Fashion Chic", template.get("layout_style"))
    check("Hero section enabled", "hero" in (template.get("sections") or []))
    check("About section enabled", "about" in (template.get("sections") or []))
    check("Contact section enabled", "contact" in (template.get("sections") or []))
    check("Products section enabled", "products" in (template.get("sections") or []))
    check("Phone present", bool(info.get("phone")), info.get("phone"))
    check("Social links present", bool(info.get("social_links")), str(list(info.get("social_links",{}).keys())))
    check("Working hours present", len(info.get("working_hours",{})) >= 5, f"{len(info.get('working_hours',{}))} days")
    check("Products on storefront", len(prods) >= 2, f"{len(prods)} products")
    for p in prods:
        print(f"         Product: {p.get('name')} | NGN {p.get('price')}")

# 3. Marketplace products
r = requests.get(f"{BASE}/marketplace/products")
check("Marketplace products endpoint works", r.status_code == 200)
all_p = r.json().get("products", [])
vendor_p = [p for p in all_p if SLUG in str(p.get("storefront_slug",""))]
check("Vendor products in marketplace", len(vendor_p) >= 2, f"{len(vendor_p)} found")
for p in vendor_p:
    print(f"         Marketplace: {p.get('name')} | Store: {p.get('storefront_slug')}")

# 4. Marketplace storefronts
r = requests.get(f"{BASE}/marketplace/storefronts")
check("Marketplace storefronts endpoint works", r.status_code == 200)
all_s = r.json().get("storefronts", [])
our_s = [s for s in all_s if s.get("store_slug") == SLUG]
check("Storefront in marketplace stores", len(our_s) >= 1, f"slug={SLUG}")
if our_s:
    s = our_s[0]
    check("Store is verified", s.get("is_verified") == True)
    check("Store is published", s.get("is_published") == True)
    check("Store is live", s.get("is_live") == True)

# 5. Visibility rules
# 5a. Unpublish → disappears
EMAIL = "stillwalker689@gmail.com"
r = requests.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": "123456789Still"})
token = r.json().get("access_token")
h = {"Authorization": f"Bearer {token}"}

requests.patch(f"{BASE}/vendor/update-settings", json={"is_published": False}, headers=h)
r = requests.get(f"{BASE}/marketplace/store/{SLUG}")
check("Unpublished → 202 offline", r.status_code == 202, r.json().get("status"))
r = requests.get(f"{BASE}/marketplace/storefronts")
gone = all(s.get("store_slug") != SLUG for s in r.json().get("storefronts", []))
check("Unpublished → not in marketplace stores", gone)

# 5b. Re-publish → back live
requests.patch(f"{BASE}/vendor/update-settings", json={"is_published": True}, headers=h)
r = requests.get(f"{BASE}/marketplace/store/{SLUG}")
check("Re-published → 200 live", r.status_code == 200)

# 6. URL format
check("Slug is URL-safe", bool(SLUG) and SLUG == SLUG.lower().replace(" ", "-"), SLUG)
print(f"\n  Storefront URL  : siiqo.com/{SLUG}")
print(f"  Subdomain URL   : {SLUG}.siiqo.com  (requires DNS wildcard *.siiqo.com)")
print(f"  API endpoint    : /api/marketplace/store/{SLUG}")

# 7. Categories
r = requests.get(f"{BASE}/marketplace/categories")
check("Categories endpoint works", r.status_code == 200)
cats = r.json()
check("Categories returned", len(cats) > 0, f"{len(cats)} categories")

# Summary
print(f"\n{'='*65}")
print(f"  RESULTS: {PASS} passed | {FAIL} failed")
print(f"{'='*65}")
if FAIL == 0:
    print("  ALL CHECKS PASSED - Platform is working correctly")
else:
    print(f"  {FAIL} check(s) need attention")
print()
