# -*- coding: utf-8 -*-
"""Update storefront branding with complete customization"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import requests, json

BASE = "http://127.0.0.1:5000/api"
EMAIL = "stillwalker689@gmail.com"
PASSWORD = "123456789Still"

print("\n" + "="*65)
print("  STOREFRONT BRANDING UPDATE")
print("="*65)

# 1. Login
print("\n[1] Logging in...")
r = requests.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": PASSWORD})
if r.status_code != 200:
    print(f"    ERROR: Login failed - {r.json()}")
    sys.exit(1)

token = r.json().get("access_token")
headers = {"Authorization": f"Bearer {token}"}
print(f"    SUCCESS: Logged in as {EMAIL}")

# 2. Update storefront with complete branding
print("\n[2] Updating storefront branding...")

branding_data = {
    "store_description": "Premium fashion and lifestyle products for the modern Nigerian. Quality you can trust, style you will love.",
    "phone": "+2348012345678",
    "website": "https://stillwalker-boutique.siiqo.com",
    "template_options": {
        "hero_heading": "Style That Speaks",
        "hero_subheading": "Discover unique fashion pieces crafted with care",
        "hero_cta": "Shop Collection",
        "layout_style": "Fashion Chic",
        "sections": ["hero", "products", "about", "reviews", "contact", "hours"],
        "about_text": "Stillwalker Boutique brings you carefully curated fashion and lifestyle products. We believe in quality, authenticity, and style that speaks to the modern Nigerian.",
        "primary_color": "#2C3E50",
        "secondary_color": "#E74C3C",
        "font_family": "Playfair Display"
    },
    "social_links": {
        "instagram": "https://instagram.com/stillwalker_boutique",
        "facebook": "https://facebook.com/stillwalkerboutique",
        "twitter": "https://twitter.com/stillwalker_ng",
        "whatsapp": "https://wa.me/2348012345678"
    },
    "working_hours": {
        "monday": "9:00 AM - 6:00 PM",
        "tuesday": "9:00 AM - 6:00 PM",
        "wednesday": "9:00 AM - 6:00 PM",
        "thursday": "9:00 AM - 6:00 PM",
        "friday": "9:00 AM - 6:00 PM",
        "saturday": "10:00 AM - 4:00 PM",
        "sunday": "Closed"
    }
}

r = requests.patch(f"{BASE}/vendor/update-settings", json=branding_data, headers=headers)
if r.status_code != 200:
    print(f"    ERROR: Update failed - {r.json()}")
    sys.exit(1)

print(f"    SUCCESS: Branding updated")
result = r.json()
print(f"    Published: {result.get('is_published')}")
print(f"    Live: {result.get('is_live')}")

# 3. Verify the update
print("\n[3] Verifying storefront...")
r = requests.get(f"{BASE}/marketplace/store/stillwalker-boutique")
if r.status_code != 200:
    print(f"    ERROR: Storefront not accessible - {r.json()}")
    sys.exit(1)

data = r.json()
info = data.get("store_info", {})
template = info.get("template_options", {})
socials = info.get("social_links", {})
hours = info.get("working_hours", {})

print(f"    Store Name: {info.get('store_name')}")
print(f"    Description: {info.get('store_description')[:50]}...")
print(f"    Phone: {info.get('phone')}")
print(f"    Website: {info.get('website')}")
print(f"    Hero Heading: {template.get('hero_heading')}")
print(f"    Hero CTA: {template.get('hero_cta')}")
print(f"    Layout Style: {template.get('layout_style')}")
print(f"    Sections: {template.get('sections')}")
print(f"    Social Links: {list(socials.keys())}")
print(f"    Working Hours: {len(hours)} days configured")
print(f"    Products: {data.get('product_count')}")

print("\n" + "="*65)
print("  BRANDING UPDATE COMPLETE")
print("="*65 + "\n")
