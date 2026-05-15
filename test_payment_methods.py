"""
Test script for payment methods (ESCROW and POD)

This script tests:
1. Checkout with ESCROW payment method
2. Checkout with POD payment method
3. POD payment confirmation
4. Withdrawal flow

Run this after applying the database migration.
"""
import requests
import json

# Configuration
BASE_URL = "http://localhost:5000"
BUYER_TOKEN = "YOUR_BUYER_JWT_TOKEN"
VENDOR_TOKEN = "YOUR_VENDOR_JWT_TOKEN"

headers_buyer = {
    "Authorization": f"Bearer {BUYER_TOKEN}",
    "Content-Type": "application/json"
}

headers_vendor = {
    "Authorization": f"Bearer {VENDOR_TOKEN}",
    "Content-Type": "application/json"
}


def test_escrow_checkout():
    """Test checkout with ESCROW payment method"""
    print("\n" + "="*60)
    print("TEST 1: Checkout with ESCROW")
    print("="*60)
    
    response = requests.post(
        f"{BASE_URL}/cart/checkout",
        headers=headers_buyer,
        json={"payment_method": "ESCROW"}
    )
    
    print(f"Status Code: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    
    if response.status_code == 200:
        data = response.json()
        if data.get('orders'):
            order = data['orders'][0]
            print(f"\n✅ ESCROW order created successfully!")
            print(f"   Order ID: {order['order_id']}")
            print(f"   Payment Method: {order.get('payment_method', 'ESCROW')}")
            print(f"   Escrow Transaction: {order.get('escrow_txn')}")
            return order['order_id']
    else:
        print(f"\n❌ ESCROW checkout failed: {response.json().get('message')}")
    
    return None


def test_pod_checkout():
    """Test checkout with POD payment method"""
    print("\n" + "="*60)
    print("TEST 2: Checkout with POD")
    print("="*60)
    
    response = requests.post(
        f"{BASE_URL}/cart/checkout",
        headers=headers_buyer,
        json={"payment_method": "POD"}
    )
    
    print(f"Status Code: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    
    if response.status_code == 200:
        data = response.json()
        if data.get('orders'):
            order = data['orders'][0]
            print(f"\n✅ POD order created successfully!")
            print(f"   Order ID: {order['order_id']}")
            print(f"   Payment Method: {order.get('payment_method', 'POD')}")
            print(f"   Status: {order.get('status')}")
            return order['order_id']
    else:
        print(f"\n❌ POD checkout failed: {response.json().get('message')}")
    
    return None


def test_pod_payments_list():
    """Test getting POD payments list"""
    print("\n" + "="*60)
    print("TEST 3: Get POD Payments List")
    print("="*60)
    
    response = requests.get(
        f"{BASE_URL}/withdrawal/pod-payments?status=pending",
        headers=headers_vendor
    )
    
    print(f"Status Code: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    
    if response.status_code == 200:
        data = response.json()
        payments = data.get('payments', [])
        print(f"\n✅ Found {len(payments)} pending POD payments")
        return payments
    else:
        print(f"\n❌ Failed to get POD payments")
    
    return []


def test_pod_confirmation(order_id):
    """Test POD payment confirmation"""
    print("\n" + "="*60)
    print(f"TEST 4: Confirm POD Payment for Order #{order_id}")
    print("="*60)
    
    response = requests.post(
        f"{BASE_URL}/withdrawal/pod-payments/{order_id}/confirm",
        headers=headers_vendor,
        json={
            "payment_method": "CASH",
            "notes": "Received cash at delivery"
        }
    )
    
    print(f"Status Code: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    
    if response.status_code == 200:
        print(f"\n✅ POD payment confirmed successfully!")
    else:
        print(f"\n❌ POD confirmation failed: {response.json().get('message')}")


def test_vendor_balance():
    """Test getting vendor balance"""
    print("\n" + "="*60)
    print("TEST 5: Get Vendor Balance")
    print("="*60)
    
    response = requests.get(
        f"{BASE_URL}/withdrawal/balance",
        headers=headers_vendor
    )
    
    print(f"Status Code: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    
    if response.status_code == 200:
        data = response.json()
        print(f"\n✅ Vendor Balance:")
        print(f"   Available: ₦{data.get('balance')}")
        print(f"   Pending Withdrawals: ₦{data.get('pending_withdrawals')}")
        print(f"   Available to Withdraw: ₦{data.get('available')}")
    else:
        print(f"\n❌ Failed to get balance")


def test_bank_accounts():
    """Test getting bank accounts"""
    print("\n" + "="*60)
    print("TEST 6: Get Bank Accounts")
    print("="*60)
    
    response = requests.get(
        f"{BASE_URL}/withdrawal/bank-accounts",
        headers=headers_vendor
    )
    
    print(f"Status Code: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    
    if response.status_code == 200:
        data = response.json()
        accounts = data.get('accounts', [])
        print(f"\n✅ Found {len(accounts)} bank account(s)")
        for acc in accounts:
            print(f"   - {acc['bank_name']}: {acc['account_number']} ({acc['account_name']})")
    else:
        print(f"\n❌ Failed to get bank accounts")


def run_all_tests():
    """Run all tests"""
    print("\n" + "="*60)
    print("PAYMENT METHODS TEST SUITE")
    print("="*60)
    print("\nMake sure you have:")
    print("1. Applied the database migration")
    print("2. Updated BUYER_TOKEN and VENDOR_TOKEN in this script")
    print("3. Added items to the buyer's cart")
    print("\nStarting tests in 3 seconds...")
    
    import time
    time.sleep(3)
    
    # Test ESCROW checkout
    escrow_order_id = test_escrow_checkout()
    
    # Test POD checkout
    pod_order_id = test_pod_checkout()
    
    # Test POD payments list
    test_pod_payments_list()
    
    # Test POD confirmation (if we created a POD order)
    if pod_order_id:
        test_pod_confirmation(pod_order_id)
    
    # Test vendor balance
    test_vendor_balance()
    
    # Test bank accounts
    test_bank_accounts()
    
    print("\n" + "="*60)
    print("ALL TESTS COMPLETED")
    print("="*60)


if __name__ == "__main__":
    print("\n⚠️  IMPORTANT: Update BUYER_TOKEN and VENDOR_TOKEN before running!")
    print("To get tokens:")
    print("1. Login as buyer: POST /auth/login")
    print("2. Login as vendor: POST /auth/login")
    print("3. Copy the access_token from each response")
    print("\nPress Enter to continue or Ctrl+C to cancel...")
    input()
    
    run_all_tests()
