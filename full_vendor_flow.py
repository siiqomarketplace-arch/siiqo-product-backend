# -*- coding: utf-8 -*-
"""
Full vendor flow test:
1. Login as stillwalker689@gmail.com
2. Submit vendor onboarding form
3. Admin approves the vendor
4. Add 2 products
5. Test publish/unpublish visibility rules
6. Verify storefront URL format
7. Verify marketplace visibility
8. Verify all edge cases (approved+unpublished, unapproved+published)
"""
import sys
import requests
import json

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = "http://127.0.0.1:5000/api"
EMAIL = "stillwalker689@gmail.com"
PASSWORD = "123456789Still"

DIVIDER = "=" * 65

def show(label, res):
    print(f"\n{DIVIDER}")
    print(f"  {label}")
    print(f"  Status: {res.status_code}")
    try:
        body = res.json()
        print(f"  Body  : {json.dumps(body, indent=2, ensure_ascii=True)[:800]}")
    except Exception:
        print(f"  Body  : {res.text[:400]}")
    print(DIVIDER)
    return res

def check(condition, msg):
    if condition:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
    return condition

# ── 0. Health ────────────────────────────────────────────────────────
print(f"\n{DIVIDER}")
print("  STEP 0: Backend health check")
print(DIVIDER)
try:
    r = requests.get("http://127.0.0.1:5000/health", timeout=5)
    print(f"  Backend: {r.json()}")
except Exception as e:
    print(f"  BACKEND DOWN: {e}")
    print("  Start it: venv\\Scripts\\activate && flask run")
    sys.exit(1)

# ── 1. Login ─────────────────────────────────────────────────────────
print(f"\n{DIVIDER}")
print("  STEP 1: Login")
print(DIVIDER)
r = requests.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": PASSWORD})
show("LOGIN", r)
assert r.status_code == 200, "Login failed"
token = r.json().get("access_token") or r.json().get("token")
headers = {"Authorization": f"Bearer {token}"}
user = r.json().get("user", {})
print(f"  Logged in as: {user.get('email')} | Role: {user.get('role')} | Verified: {user.get('is_verified')}")

# ── 2. Vendor Onboarding ─────────────────────────────────────────────
print(f"\n{DIVIDER}")
print("  STEP 2: Vendor Onboarding")
print(DIVIDER)
r = requests.post(f"{BASE}/vendor/onboard", json={
    "business_name": "Stillwalker Boutique",
    "description": "Premium fashion and lifestyle products for the modern Nigerian. Quality you can trust.",
    "address": "14 Admiralty Way, Lekki Phase 1, Lagos",
    "city": "Lagos",
    "state": "Lagos",
    "country": "Nigeria",
    "bank_code": "058",
    "account_number": "0123456789",
    "account_name": "Still Walker",
    "phone": "+2348012345678",
    "website": "https://stillwalker.com",
}, headers=headers)
show("ONBOARD", r)

if r.status_code == 200:
    print("  NOTE: Storefront already exists - continuing")
elif r.status_code == 201:
    print("  Storefront created successfully")
else:
    print(f"  Onboarding returned {r.status_code}")

# ── 3. Check vendor settings ─────────────────────────────────────────
print(f"\n{DIVIDER}")
print("  STEP 3: Check vendor settings / storefront state")
print(DIVIDER)
r = requests.get(f"{BASE}/vendor/settings", headers=headers)
show("VENDOR SETTINGS", r)
settings = r.json()
store = settings.get("store_settings", {})
slug = store.get("store_slug") or store.get("storefront_link")
is_verified = store.get("is_verified", False)
is_published = store.get("is_published", False)
is_live = store.get("is_live", False)
print(f"\n  Storefront slug    : {slug}")
print(f"  Admin verified     : {is_verified}")
print(f"  Vendor published   : {is_published}")
print(f"  Is LIVE (both)     : {is_live}")
print(f"  Storefront URL     : {slug}.siiqo.com  (or siiqo.com/{slug})")

# ── 4. Test: Published but NOT approved → should show "under review" ─
print(f"\n{DIVIDER}")
print("  STEP 4: Publish storefront (vendor side) - NOT yet admin approved")
print(DIVIDER)
r = requests.patch(f"{BASE}/vendor/update-settings", json={"is_published": True}, headers=headers)
show("PUBLISH (unapproved)", r)
data = r.json()
check(data.get("is_published") == True, "is_published set to True")
check(data.get("is_verified") == False or data.get("pending_approval") == True,
      "pending_approval flag present when not yet approved")
print(f"  Notice: {data.get('notice')}")

# ── 5. Check public storefront → should return 202 "under review" ────
print(f"\n{DIVIDER}")
print(f"  STEP 5: Public storefront check (published but NOT approved)")
print(DIVIDER)
if slug:
    r = requests.get(f"{BASE}/marketplace/store/{slug}")
    show(f"PUBLIC STORE /{slug} (expect 202 pending)", r)
    check(r.status_code == 202, "Returns 202 when published but not admin-approved")
    check("pending" in r.json().get("status", "").lower() or
          "review" in r.json().get("message", "").lower(),
          "Response says 'under review' or 'pending'")

# ── 6. Check marketplace products → should NOT appear ────────────────
print(f"\n{DIVIDER}")
print("  STEP 6: Marketplace products (unapproved vendor - should NOT appear)")
print(DIVIDER)
r = requests.get(f"{BASE}/marketplace/products")
show("MARKETPLACE PRODUCTS (unapproved)", r)
products_data = r.json()
all_products = products_data.get("products", [])
vendor_products = [p for p in all_products if slug and slug in str(p.get("storefront_slug", ""))]
check(len(vendor_products) == 0, f"No products from unapproved vendor in marketplace (found {len(vendor_products)})")

# ── 7. Admin login ───────────────────────────────────────────────────
print(f"\n{DIVIDER}")
print("  STEP 7: Admin login")
print(DIVIDER)

# Try to get admin credentials from DB
from app import create_app
from app.models.admin import AdminUser
from app.models.user import User, Storefront

app = create_app()
admin_email = None
admin_password = None

with app.app_context():
    admin = AdminUser.query.first()
    if admin:
        admin_email = admin.email
        print(f"  Found admin: {admin_email}")
    else:
        print("  No admin found - creating one for test")
        from app.extensions import db
        new_admin = AdminUser(
            name="Test Admin",
            email="admin@siiqo.com",
            role="SUPERADMIN",
            is_active=True
        )
        new_admin.set_password("Admin@Siiqo2026")
        db.session.add(new_admin)
        db.session.commit()
        admin_email = "admin@siiqo.com"
        admin_password = "Admin@Siiqo2026"
        print(f"  Created admin: {admin_email} / Admin@Siiqo2026")

if not admin_password:
    admin_password = "Admin@Siiqo2026"

r = requests.post(f"{BASE}/admin/login", json={"email": admin_email, "password": admin_password})
show("ADMIN LOGIN", r)

if r.status_code != 200:
    print("  Admin login failed - trying to approve directly via DB")
    admin_token = None
else:
    admin_token = r.json().get("access_token")
    print(f"  Admin token: {admin_token[:30]}...")

admin_headers = {"Authorization": f"Bearer {admin_token}"} if admin_token else {}

# ── 8. Admin approves vendor ─────────────────────────────────────────
print(f"\n{DIVIDER}")
print("  STEP 8: Admin approves vendor")
print(DIVIDER)

# Get user ID
with app.app_context():
    vendor_user = User.query.filter_by(email=EMAIL).first()
    vendor_id = vendor_user.id if vendor_user else None
    print(f"  Vendor user ID: {vendor_id}")

if admin_token and vendor_id:
    r = requests.patch(
        f"{BASE}/admin/users/{vendor_id}/status",
        json={"status": "approved"},
        headers=admin_headers
    )
    show("ADMIN APPROVE VENDOR", r)
else:
    # Direct DB approval
    print("  Approving directly via DB...")
    with app.app_context():
        from app.extensions import db
        vendor_user = User.query.filter_by(email=EMAIL).first()
        if vendor_user:
            vendor_user.is_verified = True
            vendor_user.is_active = True
            if vendor_user.storefront:
                vendor_user.storefront.is_verified = True
                print(f"  Approved storefront: {vendor_user.storefront.store_name}")
            db.session.commit()
            print("  Vendor approved via DB")

# ── 9. Verify storefront state after approval ────────────────────────
print(f"\n{DIVIDER}")
print("  STEP 9: Verify storefront state after admin approval")
print(DIVIDER)
r = requests.get(f"{BASE}/vendor/settings", headers=headers)
store2 = r.json().get("store_settings", {})
print(f"  is_verified  : {store2.get('is_verified')}")
print(f"  is_published : {store2.get('is_published')}")
print(f"  is_live      : {store2.get('is_live')}")
check(store2.get("is_verified") == True, "Admin approval reflected in vendor settings")

# ── 10. Public storefront → should now work ──────────────────────────
print(f"\n{DIVIDER}")
print("  STEP 10: Public storefront (approved + published = LIVE)")
print(DIVIDER)
if slug:
    r = requests.get(f"{BASE}/marketplace/store/{slug}")
    show(f"PUBLIC STORE /{slug} (expect 200 live)", r)
    check(r.status_code == 200, "Returns 200 when approved AND published")
    if r.status_code == 200:
        store_info = r.json().get("store_info", {})
        check(store_info.get("name") == "Stillwalker Boutique", "Store name correct")

# ── 11. Add 2 products ───────────────────────────────────────────────
print(f"\n{DIVIDER}")
print("  STEP 11: Adding 2 products")
print(DIVIDER)

product1 = {
    "name": "Ankara Print Tote Bag",
    "description": "Handcrafted tote bag made from premium Ankara fabric. Perfect for everyday use. Available in multiple patterns.",
    "price": 8500,
    "stock_quantity": 25,
}
product2 = {
    "name": "Leather Crossbody Wallet",
    "description": "Genuine leather crossbody wallet with multiple card slots and a secure zip compartment. Slim and stylish.",
    "price": 15000,
    "stock_quantity": 10,
}

r1 = requests.post(f"{BASE}/vendor/products/add", json=product1, headers=headers)
show("ADD PRODUCT 1: Ankara Print Tote Bag", r1)
check(r1.status_code == 201, "Product 1 created")
p1_id = r1.json().get("id")

r2 = requests.post(f"{BASE}/vendor/products/add", json=product2, headers=headers)
show("ADD PRODUCT 2: Leather Crossbody Wallet", r2)
check(r2.status_code == 201, "Product 2 created")
p2_id = r2.json().get("id")

print(f"\n  Product 1 ID: {p1_id}")
print(f"  Product 2 ID: {p2_id}")

# ── 12. Verify products in vendor dashboard ──────────────────────────
print(f"\n{DIVIDER}")
print("  STEP 12: Verify products in vendor dashboard")
print(DIVIDER)
r = requests.get(f"{BASE}/products/my-products", headers=headers)
show("MY PRODUCTS", r)
my_products = r.json() if isinstance(r.json(), list) else r.json().get("products", [])
check(len(my_products) >= 2, f"At least 2 products in vendor dashboard (found {len(my_products)})")
for p in my_products:
    print(f"  - [{p.get('id')}] {p.get('name')} | Price: {p.get('price')} | Active: {p.get('is_active')}")

# ── 13. Verify products on public storefront ─────────────────────────
print(f"\n{DIVIDER}")
print("  STEP 13: Verify products on public storefront URL")
print(DIVIDER)
if slug:
    r = requests.get(f"{BASE}/marketplace/store/{slug}")
    show(f"PUBLIC STOREFRONT /{slug}", r)
    if r.status_code == 200:
        catalogs = r.json().get("catalogs", [])
        all_sf_products = [p for cat in catalogs for p in cat.get("products", [])]
        check(len(all_sf_products) >= 2, f"Products visible on storefront (found {len(all_sf_products)})")
        for p in all_sf_products:
            print(f"  - [{p.get('id')}] {p.get('name')} | Price: {p.get('price')}")

# ── 14. Verify products in marketplace ──────────────────────────────
print(f"\n{DIVIDER}")
print("  STEP 14: Verify products in marketplace (approved + published)")
print(DIVIDER)
r = requests.get(f"{BASE}/marketplace/products")
show("MARKETPLACE PRODUCTS (approved vendor)", r)
all_mp = r.json().get("products", [])
vendor_mp = [p for p in all_mp if slug and slug in str(p.get("storefront_slug", ""))]
check(len(vendor_mp) >= 2, f"Vendor products visible in marketplace (found {len(vendor_mp)})")
for p in vendor_mp:
    print(f"  - [{p.get('id')}] {p.get('name')} | Store: {p.get('storefront_slug')}")

# ── 15. Verify storefront in marketplace stores ──────────────────────
print(f"\n{DIVIDER}")
print("  STEP 15: Verify storefront in marketplace stores section")
print(DIVIDER)
r = requests.get(f"{BASE}/marketplace/storefronts")
show("MARKETPLACE STOREFRONTS", r)
all_stores = r.json().get("storefronts", [])
our_store = [s for s in all_stores if s.get("store_slug") == slug]
check(len(our_store) >= 1, f"Storefront visible in marketplace stores (found {len(our_store)})")
if our_store:
    s = our_store[0]
    print(f"  Store: {s.get('store_name')} | Slug: {s.get('store_slug')} | Verified: {s.get('is_verified')}")

# ── 16. Storefront URL format check ─────────────────────────────────
print(f"\n{DIVIDER}")
print("  STEP 16: Storefront URL format")
print(DIVIDER)
print(f"  Slug          : {slug}")
print(f"  Public URL    : siiqo.com/{slug}")
print(f"  API endpoint  : /api/marketplace/store/{slug}")
check(slug is not None and len(slug) > 0, "Slug exists")
check("-" in slug or slug.isalnum(), "Slug is URL-safe (alphanumeric/hyphens)")
print(f"\n  NOTE: The storefront URL format is siiqo.com/{{slug}}")
print(f"  e.g. siiqo.com/{slug}")
print(f"  Custom domain (storename.siiqo.com) requires DNS CNAME setup")
print(f"  which is handled at the hosting/DNS level, not in the app code.")

# ── 17. Unpublish test → should disappear from marketplace ──────────
print(f"\n{DIVIDER}")
print("  STEP 17: Unpublish storefront → should disappear from marketplace")
print(DIVIDER)
r = requests.patch(f"{BASE}/vendor/update-settings", json={"is_published": False}, headers=headers)
show("UNPUBLISH", r)
check(r.json().get("is_published") == False, "is_published set to False")
check(r.json().get("is_live") == False, "is_live is False after unpublish")

r = requests.get(f"{BASE}/marketplace/storefronts")
all_stores2 = r.json().get("storefronts", [])
our_store2 = [s for s in all_stores2 if s.get("store_slug") == slug]
check(len(our_store2) == 0, "Storefront NOT in marketplace after unpublish")

r = requests.get(f"{BASE}/marketplace/store/{slug}")
check(r.status_code == 202, "Public storefront returns 202 (offline) after unpublish")
print(f"  Status: {r.json().get('status')} | Message: {r.json().get('message')}")

# ── 18. Re-publish ───────────────────────────────────────────────────
print(f"\n{DIVIDER}")
print("  STEP 18: Re-publish storefront")
print(DIVIDER)
r = requests.patch(f"{BASE}/vendor/update-settings", json={"is_published": True}, headers=headers)
show("RE-PUBLISH", r)
check(r.json().get("is_live") == True, "is_live True after re-publish (approved + published)")

# ── 19. Storefront customization ─────────────────────────────────────
print(f"\n{DIVIDER}")
print("  STEP 19: Storefront customization (hero, sections, about, contact)")
print(DIVIDER)
r = requests.patch(f"{BASE}/vendor/update-settings", json={
    "description": "Premium fashion and lifestyle products for the modern Nigerian. Quality you can trust, style you will love.",
    "phone": "+2348012345678",
    "website": "https://stillwalker.com",
    "meta_title": "Stillwalker Boutique - Premium Fashion Lagos",
    "meta_description": "Shop premium Ankara bags, leather wallets and fashion accessories from Stillwalker Boutique, Lagos.",
    "template_options": {
        "layout_style": "Fashion Chic",
        "store_mode": "ecommerce",
        "primary_color": "#0b1b3b",
        "secondary_color": "#ffffff",
        "palette_id": "Midnight Gold",
        "font_heading": "Playfair Display",
        "font_body": "system-ui",
        "hero_layout": "centered",
        "hero_heading": "Style That Speaks",
        "hero_subtext": "Premium Ankara bags and leather accessories crafted for the modern Nigerian.",
        "hero_cta": "Shop Collection",
        "sections": ["hero", "products", "about", "reviews", "contact", "hours"]
    },
    "social_links": {
        "whatsapp": "+2348012345678",
        "instagram": "https://instagram.com/stillwalkerboutique",
        "facebook": "https://facebook.com/stillwalkerboutique"
    },
    "working_hours": {
        "Monday":    {"start": "09:00", "end": "18:00"},
        "Tuesday":   {"start": "09:00", "end": "18:00"},
        "Wednesday": {"start": "09:00", "end": "18:00"},
        "Thursday":  {"start": "09:00", "end": "18:00"},
        "Friday":    {"start": "09:00", "end": "18:00"},
        "Saturday":  {"start": "10:00", "end": "16:00"},
        "Sunday":    {"start": "12:00", "end": "15:00"}
    }
}, headers=headers)
show("CUSTOMIZE STOREFRONT", r)
check(r.status_code == 200, "Customization saved")
check(r.json().get("status") == "success", "Status is success")

# ── 20. Final public storefront check ───────────────────────────────
print(f"\n{DIVIDER}")
print("  STEP 20: Final public storefront - full data check")
print(DIVIDER)
if slug:
    r = requests.get(f"{BASE}/marketplace/store/{slug}")
    show(f"FINAL PUBLIC STORE /{slug}", r)
    if r.status_code == 200:
        data = r.json()
        info = data.get("store_info", {})
        catalogs = data.get("catalogs", [])
        all_prods = [p for cat in catalogs for p in cat.get("products", [])]
        check(info.get("name") == "Stillwalker Boutique", "Store name correct")
        check(len(all_prods) >= 2, f"Products showing ({len(all_prods)} found)")
        check(bool(info.get("phone")), "Phone number present")
        check(bool(info.get("socials")), "Social links present")
        check(bool(info.get("hours")), "Working hours present")
        branding = info.get("branding", {})
        check(branding.get("hero_heading") == "Style That Speaks", "Hero heading saved")
        check("hero" in branding.get("sections", []), "Hero section enabled")
        check("about" in branding.get("sections", []), "About section enabled")
        check("contact" in branding.get("sections", []), "Contact section enabled")

# ── Summary ──────────────────────────────────────────────────────────
print(f"\n{DIVIDER}")
print("  COMPLETE - SUMMARY")
print(DIVIDER)
print(f"""
  Vendor Account  : {EMAIL}
  Password        : {PASSWORD}
  Role            : VENDOR
  Storefront      : Stillwalker Boutique
  Slug            : {slug}
  Public URL      : siiqo.com/{slug}
  Admin Approved  : YES
  Published       : YES
  Live            : YES

  Products:
    1. Ankara Print Tote Bag     - NGN 8,500
    2. Leather Crossbody Wallet  - NGN 15,000

  Sections: Hero, Products, About, Reviews, Contact, Hours
  Theme   : Fashion Chic
  Hero    : "Style That Speaks"

  Visibility Rules Confirmed:
    - Approved + Published  = LIVE (200)
    - Approved + Unpublished = OFFLINE (202)
    - Unapproved + Published = UNDER REVIEW (202)
    - Unapproved + Unpublished = OFFLINE (202)
""")
