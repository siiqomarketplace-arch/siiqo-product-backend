# -*- coding: utf-8 -*-
"""Test frontend pages to verify they're loading correctly"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import requests

FRONTEND = "http://localhost:3000"
BACKEND = "http://127.0.0.1:5000/api"

print("\n" + "="*65)
print("  FRONTEND VERIFICATION")
print("="*65)

tests = []

# 1. Homepage
print("\n[1] Testing Homepage...")
try:
    r = requests.get(FRONTEND, timeout=10)
    status = "✅ PASS" if r.status_code == 200 else f"❌ FAIL ({r.status_code})"
    print(f"    {status} - Homepage")
    tests.append(("Homepage", r.status_code == 200))
except Exception as e:
    print(f"    ❌ FAIL - Homepage: {str(e)}")
    tests.append(("Homepage", False))

# 2. Marketplace
print("\n[2] Testing Marketplace...")
try:
    r = requests.get(f"{FRONTEND}/marketplace", timeout=10)
    status = "✅ PASS" if r.status_code == 200 else f"❌ FAIL ({r.status_code})"
    print(f"    {status} - Marketplace page")
    tests.append(("Marketplace", r.status_code == 200))
except Exception as e:
    print(f"    ❌ FAIL - Marketplace: {str(e)}")
    tests.append(("Marketplace", False))

# 3. Storefront
print("\n[3] Testing Storefront...")
try:
    r = requests.get(f"{FRONTEND}/stillwalker-boutique", timeout=10)
    status = "✅ PASS" if r.status_code == 200 else f"❌ FAIL ({r.status_code})"
    print(f"    {status} - Storefront page")
    tests.append(("Storefront", r.status_code == 200))
    
    # Check if it's actually rendering the storefront
    if r.status_code == 200:
        content = r.text.lower()
        has_stillwalker = "stillwalker" in content
        has_boutique = "boutique" in content
        print(f"    Content check: {'✅' if has_stillwalker else '❌'} Contains 'stillwalker'")
        print(f"    Content check: {'✅' if has_boutique else '❌'} Contains 'boutique'")
except Exception as e:
    print(f"    ❌ FAIL - Storefront: {str(e)}")
    tests.append(("Storefront", False))

# 4. About page
print("\n[4] Testing About page...")
try:
    r = requests.get(f"{FRONTEND}/about", timeout=10)
    status = "✅ PASS" if r.status_code == 200 else f"❌ FAIL ({r.status_code})"
    print(f"    {status} - About page")
    tests.append(("About", r.status_code == 200))
except Exception as e:
    print(f"    ❌ FAIL - About: {str(e)}")
    tests.append(("About", False))

# 5. Login page
print("\n[5] Testing Login page...")
try:
    r = requests.get(f"{FRONTEND}/login", timeout=10)
    status = "✅ PASS" if r.status_code == 200 else f"❌ FAIL ({r.status_code})"
    print(f"    {status} - Login page")
    tests.append(("Login", r.status_code == 200))
except Exception as e:
    print(f"    ❌ FAIL - Login: {str(e)}")
    tests.append(("Login", False))

# 6. Signup page
print("\n[6] Testing Signup page...")
try:
    r = requests.get(f"{FRONTEND}/signup", timeout=10)
    status = "✅ PASS" if r.status_code == 200 else f"❌ FAIL ({r.status_code})"
    print(f"    {status} - Signup page")
    tests.append(("Signup", r.status_code == 200))
except Exception as e:
    print(f"    ❌ FAIL - Signup: {str(e)}")
    tests.append(("Signup", False))

# 7. Backend API connectivity from frontend perspective
print("\n[7] Testing Backend API connectivity...")
try:
    r = requests.get(f"{BACKEND}/marketplace/products", timeout=10)
    status = "✅ PASS" if r.status_code == 200 else f"❌ FAIL ({r.status_code})"
    print(f"    {status} - Backend API accessible")
    if r.status_code == 200:
        products = r.json().get("products", [])
        print(f"    Products available: {len(products)}")
        for p in products[:3]:
            print(f"      - {p.get('name')} | NGN {p.get('price')}")
    tests.append(("Backend API", r.status_code == 200))
except Exception as e:
    print(f"    ❌ FAIL - Backend API: {str(e)}")
    tests.append(("Backend API", False))

# 8. Storefront API
print("\n[8] Testing Storefront API...")
try:
    r = requests.get(f"{BACKEND}/marketplace/store/stillwalker-boutique", timeout=10)
    status = "✅ PASS" if r.status_code == 200 else f"❌ FAIL ({r.status_code})"
    print(f"    {status} - Storefront API")
    if r.status_code == 200:
        data = r.json()
        info = data.get("store_info", {})
        template = info.get("template_options", {})
        print(f"    Store: {info.get('store_name')}")
        print(f"    Hero: {template.get('hero_heading')}")
        print(f"    Products: {data.get('product_count')}")
        print(f"    Sections: {len(template.get('sections', []))}")
    tests.append(("Storefront API", r.status_code == 200))
except Exception as e:
    print(f"    ❌ FAIL - Storefront API: {str(e)}")
    tests.append(("Storefront API", False))

# Summary
print("\n" + "="*65)
passed = sum(1 for _, result in tests if result)
total = len(tests)
print(f"  RESULTS: {passed}/{total} tests passed")
print("="*65)

if passed == total:
    print("  ✅ ALL TESTS PASSED - Frontend is working correctly!")
else:
    print(f"  ⚠️  {total - passed} test(s) need attention")
    print("\n  Failed tests:")
    for name, result in tests:
        if not result:
            print(f"    ❌ {name}")

print("\n  Frontend URLs:")
print(f"    Homepage    : {FRONTEND}")
print(f"    Marketplace : {FRONTEND}/marketplace")
print(f"    Storefront  : {FRONTEND}/stillwalker-boutique")
print(f"    Login       : {FRONTEND}/login")
print(f"    Signup      : {FRONTEND}/signup")
print()
