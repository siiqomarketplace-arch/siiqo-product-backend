#!/usr/bin/env python3
"""
audit_and_cleanup_test_data.py

Safe, transactional script to:
1. Audit (dry-run) test orders, paylinks, and ticket purchases for target test accounts:
   - okerekeinno6@gmail.com
   - tessymoses67@gmail.com
   - nspdnigeria@gmail.com
2. When run with `--execute`, cleanly and safely remove test orders, payment links, and test ticket bookings.
3. PRESERVES all events, ticket types, and storefront setups, resetting sold counters so real customers can purchase.

Usage:
  Dry-Run (Audit only, no changes):
    python3 audit_and_cleanup_test_data.py

  Execute Deletion (Permanent cleanup):
    python3 audit_and_cleanup_test_data.py --execute
"""

import sys
import argparse
from app import create_app
from app.extensions import db
from app.models.user import User, Storefront
from app.models.order import Order, OrderItem
from app.models.escrow import EscrowTransaction
from app.models.payment_link import PaymentLink
from app.models.event import Event, TicketType, TicketPurchase

TARGET_EMAILS = [
    "okerekeinno6@gmail.com",
    "tessymoses67@gmail.com",
    "nspdnigeria@gmail.com"
]

def main():
    parser = argparse.ArgumentParser(description="Audit and clean test orders/tickets for specified test vendors.")
    parser.add_argument("--execute", action="store_true", help="Perform the actual deletion. If omitted, runs in read-only dry-run mode.")
    args = parser.parse_args()

    app = create_app()

    with app.app_context():
        print("=" * 80)
        mode_label = "EXECUTION MODE (CHANGES WILL BE COMMITTED)" if args.execute else "DRY-RUN AUDIT MODE (READ ONLY — NO CHANGES)"
        print(f"SIIQO TEST DATA AUDIT & CLEANUP — {mode_label}")
        print("=" * 80)

        total_orders_found = 0
        total_paylinks_found = 0
        total_tickets_found = 0

        target_user_ids = []
        target_users = []

        for email in TARGET_EMAILS:
            user = User.query.filter(User.email.ilike(email.strip())).first()
            if not user:
                print(f"\n[-] Account not found for email: {email}")
                continue
            target_user_ids.append(user.id)
            target_users.append(user)

        if not target_user_ids:
            print("\n[!] No matching accounts found in the database.")
            return

        print(f"\n[+] Found {len(target_users)} target accounts to inspect:")
        for u in target_users:
            print(f"    - User #{u.id}: {u.email} (Name: {getattr(u, 'fullname', getattr(u, 'name', 'N/A'))})")

        print("\n" + "-" * 80)
        print("DETAILED AUDIT BY VENDOR")
        print("-" * 80)

        for user in target_users:
            print(f"\n>>> VENDOR: {user.email} (ID: {user.id})")

            # 1. Orders (as vendor or buyer)
            v_orders = Order.query.filter_by(vendor_id=user.id).all()
            b_orders = Order.query.filter(
                (Order.buyer_id == user.id) | 
                (Order.buyer_email.ilike(user.email))
            ).all()
            
            all_user_orders = list({o.id: o for o in (v_orders + b_orders)}.values())
            print(f"    * Orders: {len(all_user_orders)} (as Vendor: {len(v_orders)}, as Buyer: {len(b_orders)})")
            total_orders_found += len(all_user_orders)
            for o in all_user_orders:
                escrow_status = o.escrow.status if o.escrow else "No Escrow"
                print(f"      - Order #{o.id} | Amount: N{o.total_amount} | Status: {o.status} | Escrow: {escrow_status} | Buyer: {o.buyer_name or o.buyer_email or 'Guest'} | Created: {o.created_at}")

            # 2. Payment Links
            try:
                pls = PaymentLink.query.filter_by(vendor_id=user.id).all()
            except Exception:
                pls = []
            print(f"    * Payment Links: {len(pls)}")
            total_paylinks_found += len(pls)
            for pl in pls:
                print(f"      - PayLink #{pl.id} | Title: '{getattr(pl, 'title', getattr(pl, 'name', 'N/A'))}' | Amount: N{getattr(pl, 'amount', 0)} | Active: {getattr(pl, 'is_active', True)}")

            # 3. Events Listed (PRESERVED) & Tickets Sold
            events = Event.query.filter_by(vendor_id=user.id).all()
            print(f"    * Listed Events (PRESERVED): {len(events)}")
            for ev in events:
                purchases = TicketPurchase.query.filter_by(event_id=ev.id).order_by(TicketPurchase.id.asc()).all()
                total_tickets_found += len(purchases)
                print(f"      - Event #{ev.id}: '{ev.title}' (Slug: {ev.slug}) | Capacity: {ev.total_capacity or 'Unlimited'} | Recorded Tickets Sold: {ev.tickets_sold}")
                print(f"        -> Test Ticket Purchases on this event ({len(purchases)} total):")
                if not purchases:
                    print("           (No ticket purchases recorded for this event)")
                for tp in purchases:
                    ticket_type_name = tp.ticket_type.name if tp.ticket_type else "General"
                    order_ref = f"Order #{tp.order_id}" if tp.order_id else "Direct/Free"
                    print(f"           * Ticket ID #{tp.id:03d} | Code: {tp.ticket_code:<18} | Type: {ticket_type_name:<10} | Qty: {tp.quantity} | Paid: N{tp.price_paid:<8} | Buyer: {tp.buyer_name} <{tp.buyer_email}> | Status: {tp.status} | {order_ref} | Date: {tp.created_at}")

            # 4. Tickets purchased by this user on other events
            user_purchases = TicketPurchase.query.filter(
                (TicketPurchase.buyer_id == user.id) | 
                (TicketPurchase.buyer_email.ilike(user.email))
            ).order_by(TicketPurchase.id.asc()).all()
            print(f"    * Tickets Purchased by {user.email} as Attendee ({len(user_purchases)} total):")
            if not user_purchases:
                print("       (None)")
            for up in user_purchases:
                ev_title = up.event.title if up.event else f"Event #{up.event_id}"
                tt_name = up.ticket_type.name if up.ticket_type else "General"
                print(f"      - Ticket ID #{up.id:03d} | Event: '{ev_title}' | Code: {up.ticket_code} | Type: {tt_name} | Qty: {up.quantity} | Paid: N{up.price_paid} | Status: {up.status} | Date: {up.created_at}")

        print("\n" + "=" * 80)
        print("AUDIT SUMMARY:")
        print(f"  - Target Accounts:        {len(target_users)}")
        print(f"  - Total Orders to clean:  {total_orders_found}")
        print(f"  - Total Paylinks to clean:{total_paylinks_found}")
        print(f"  - Total Tickets to clean: {total_tickets_found}")
        print("=" * 80)

        if not args.execute:
            print("\n[i] This was a DRY RUN. No records were deleted.")
            print("[i] To delete these test orders, paylinks, and test tickets, run with:")
            print("      python3 audit_and_cleanup_test_data.py --execute\n")
            return

        # ── EXECUTION PHASE ───────────────────────────────────────────────────
        print("\n[!] STARTING SAFE TRANSACTIONAL CLEANUP...")
        try:
            # 1. Clean Ticket Purchases for target vendors' events and target buyers
            events_to_reset = set()

            # Find all ticket purchases on target events OR by target users
            target_events = Event.query.filter(Event.vendor_id.in_(target_user_ids)).all()
            for ev in target_events:
                events_to_reset.add(ev)

            deleted_tickets_count = 0
            # Delete tickets on target events
            for ev in target_events:
                tps = TicketPurchase.query.filter_by(event_id=ev.id).all()
                for tp in tps:
                    db.session.delete(tp)
                    deleted_tickets_count += 1

            # Delete tickets purchased by target users
            user_tps = TicketPurchase.query.filter(
                TicketPurchase.buyer_id.in_(target_user_ids) |
                TicketPurchase.buyer_email.in_([u.email.lower() for u in target_users])
            ).all()
            for utp in user_tps:
                if utp.event:
                    events_to_reset.add(utp.event)
                db.session.delete(utp)
                deleted_tickets_count += 1

            # Reset tickets_sold on events and quantity_sold on ticket_types
            for ev in events_to_reset:
                ev.tickets_sold = 0
                for tt in ev.ticket_types:
                    tt.quantity_sold = 0

            # 2. Clean Orders and Order Items
            target_orders = Order.query.filter(
                Order.vendor_id.in_(target_user_ids) |
                Order.buyer_id.in_(target_user_ids) |
                Order.buyer_email.in_([u.email.lower() for u in target_users])
            ).all()

            deleted_orders_count = len(target_orders)
            for o in target_orders:
                # Delete linked escrow transaction if any
                if o.escrow:
                    db.session.delete(o.escrow)
                # Delete order (cascade deletes order_items)
                db.session.delete(o)

            # 3. Clean Payment Links
            deleted_pls_count = 0
            try:
                pls = PaymentLink.query.filter(PaymentLink.vendor_id.in_(target_user_ids)).all()
                deleted_pls_count = len(pls)
                for pl in pls:
                    db.session.delete(pl)
            except Exception:
                pass

            # Commit all changes atomically
            db.session.commit()

            print("\n[SUCCESS] Cleanup committed successfully!")
            print(f"  ✓ Deleted {deleted_orders_count} test orders (and associated escrow/items).")
            print(f"  ✓ Deleted {deleted_pls_count} test payment links.")
            print(f"  ✓ Deleted {deleted_tickets_count} test tickets.")
            print(f"  ✓ Reset tickets_sold counters to 0 across {len(events_to_reset)} events (events preserved).")
            print("=" * 80)

        except Exception as e:
            db.session.rollback()
            print(f"\n[ERROR] Cleanup failed: {str(e)}")
            print("[!] Transaction rolled back completely. No database records were modified.")
            sys.exit(1)

if __name__ == "__main__":
    main()
