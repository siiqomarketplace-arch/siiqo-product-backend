#!/usr/bin/env python3
"""
Direct migration script - Run this on EB instance via SSH
This bypasses the admin endpoint and runs the migration directly
"""

import os
import sys

# Set environment to production
os.environ['FLASK_ENV'] = 'production'

from app import create_app
from app.extensions import db
from sqlalchemy import text

def run_migration():
    """Execute grants table migration directly"""
    app = create_app('production')
    
    with app.app_context():
        print("\n" + "="*60)
        print("GRANTS DATABASE MIGRATION (DIRECT)")
        print("="*60)
        
        try:
            # Check if grants table already exists
            result = db.session.execute(text("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'grants'
                )
            """))
            
            table_exists = result.scalar()
            
            if table_exists:
                print("⚠️  Grants table already exists!")
                print("\nChecking current grants...")
                result = db.session.execute(text("""
                    SELECT COUNT(*) as total,
                           COUNT(*) FILTER (WHERE status = 'open') as open,
                           COUNT(*) FILTER (WHERE featured = TRUE) as featured
                    FROM grants
                """))
                row = result.fetchone()
                print(f"  - Total grants: {row[0]}")
                print(f"  - Open grants: {row[1]}")
                print(f"  - Featured grants: {row[2]}")
                
                if row[0] > 0:
                    print("\n✓ Grants table is already populated!")
                    print("="*60)
                    return True
                else:
                    print("\nTable exists but is empty. Skipping SQL file execution.")
                    print("You may need to manually insert grant data.")
                    print("="*60)
                    return False
            
            # Read SQL migration file
            sql_file = 'migrations/create_grants_table.sql'
            print(f"\nReading migration file: {sql_file}")
            
            if not os.path.exists(sql_file):
                print(f"❌ Migration file not found: {sql_file}")
                return False
            
            with open(sql_file, 'r', encoding='utf-8') as f:
                sql_content = f.read()
            
            # Execute migration
            print("Executing migration...")
            db.session.execute(text(sql_content))
            db.session.commit()
            print("✓ Migration SQL executed successfully!")
            
            # Verify
            result = db.session.execute(text("""
                SELECT COUNT(*) as total,
                       COUNT(*) FILTER (WHERE status = 'open') as open,
                       COUNT(*) FILTER (WHERE featured = TRUE) as featured
                FROM grants
            """))
            
            row = result.fetchone()
            
            print(f"\n✓ Verification complete:")
            print(f"  - Total grants: {row[0]}")
            print(f"  - Open grants: {row[1]}")
            print(f"  - Featured grants: {row[2]}")
            
            # Show sample grants
            print("\nSample grants:")
            result = db.session.execute(text("""
                SELECT id, name, amount, status
                FROM grants
                ORDER BY created_at DESC
                LIMIT 3
            """))
            
            for row in result:
                print(f"  [{row[0]}] {row[1]} - {row[2]} ({row[3]})")
            
            print("\n" + "="*60)
            print("✓ MIGRATION COMPLETED SUCCESSFULLY!")
            print("="*60)
            
            return True
            
        except Exception as e:
            print(f"\n❌ Migration failed: {str(e)}")
            db.session.rollback()
            
            print("\nTroubleshooting:")
            print("1. Check if admin_users table exists (foreign key dependency)")
            print("2. Verify database connection settings")
            print("3. Check PostgreSQL logs")
            print("="*60)
            
            return False

if __name__ == '__main__':
    success = run_migration()
    sys.exit(0 if success else 1)
