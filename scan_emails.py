#!/usr/bin/env python3
"""
Email Validation Scanner
Scans database for invalid/malicious emails and provides cleanup options.

Usage:
    python scan_emails.py              # Scan only
    python scan_emails.py --fix        # Scan and mark invalid emails
    python scan_emails.py --delete     # Scan and delete invalid users (DANGEROUS!)
"""
import sys
import os
import argparse

# Add app to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.extensions import db
from app.middleware.security import scan_database_for_invalid_emails
from app.models.user import User


def main():
    parser = argparse.ArgumentParser(description='Scan database for invalid emails')
    parser.add_argument('--fix', action='store_true', help='Mark invalid emails for review')
    parser.add_argument('--delete', action='store_true', help='Delete users with invalid emails (DANGEROUS!)')
    parser.add_argument('--export', type=str, help='Export results to CSV file')
    args = parser.parse_args()
    
    app = create_app()
    
    with app.app_context():
        print("=" * 70)
        print("SIIQO PLATFORM — EMAIL VALIDATION SCANNER")
        print("=" * 70)
        print()
        
        # Scan database
        results = scan_database_for_invalid_emails(db.session)
        
        print(f"Total Users: {results['total_users']}")
        print(f"Invalid Emails: {results['invalid_count']}")
        print()
        
        if results['invalid_count'] == 0:
            print("✅ All emails are valid!")
            return 0
        
        print("❌ Invalid Emails Found:")
        print("-" * 70)
        
        for item in results['invalid_emails']:
            print(f"  ID: {item['id']}")
            print(f"  Email: {item['email']}")
            print(f"  Role: {item['role']}")
            print(f"  Created: {item['created_at']}")
            print()
        
        # Export to CSV
        if args.export:
            import csv
            with open(args.export, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=['id', 'email', 'role', 'created_at'])
                writer.writeheader()
                writer.writerows(results['invalid_emails'])
            print(f"✅ Results exported to {args.export}")
            print()
        
        # Fix mode: Mark emails as invalid
        if args.fix:
            print("🔧 Marking invalid emails...")
            for item in results['invalid_emails']:
                user = db.session.get(User, item['id'])
                if user:
                    # Append "[INVALID]" to email so they can't login
                    if "[INVALID]" not in user.email:
                        user.email = f"{user.email}[INVALID]"
                        user.is_active = False
                        print(f"  Marked: {item['email']}")
            
            db.session.commit()
            print(f"✅ Marked {results['invalid_count']} users as invalid")
            print()
        
        # Delete mode: Remove users (DANGEROUS!)
        if args.delete:
            print("⚠️  DELETE MODE ACTIVATED")
            print("This will PERMANENTLY DELETE users with invalid emails!")
            print()
            
            confirm = input("Type 'DELETE' to confirm: ")
            if confirm != 'DELETE':
                print("❌ Deletion cancelled")
                return 1
            
            print("🗑️  Deleting users...")
            for item in results['invalid_emails']:
                user = db.session.get(User, item['id'])
                if user:
                    db.session.delete(user)
                    print(f"  Deleted: {item['email']}")
            
            db.session.commit()
            print(f"✅ Deleted {results['invalid_count']} users")
            print()
        
        print("=" * 70)
        print("Scan complete!")
        print("=" * 70)
        
        return 0


if __name__ == '__main__':
    sys.exit(main())
