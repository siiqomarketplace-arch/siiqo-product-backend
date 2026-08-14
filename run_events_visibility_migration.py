"""
run_events_visibility_migration.py
Adds show_on_storefront and show_on_marketplace columns to the events table if missing.
"""

import os
import sys

# Ensure current directory is in python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.extensions import db
from sqlalchemy import text

app = create_app()

def run_migration():
    print("=" * 60)
    print("🎫 Running Events Visibility Columns Migration...")
    print("=" * 60)
    
    with app.app_context():
        try:
            # Check if columns exist
            sql_check = text("""
                SELECT column_name 
                from information_schema.columns 
                WHERE table_name = 'events';
            """)
            result = db.session.execute(sql_check)
            columns = [row[0] for row in result.fetchall()]
            
            if 'show_on_storefront' not in columns:
                print("➕ Adding show_on_storefront column...")
                db.session.execute(text("ALTER TABLE events ADD COLUMN show_on_storefront BOOLEAN DEFAULT TRUE;"))
            else:
                print("ℹ️ Column show_on_storefront already exists.")
                
            if 'show_on_marketplace' not in columns:
                print("➕ Adding show_on_marketplace column...")
                db.session.execute(text("ALTER TABLE events ADD COLUMN show_on_marketplace BOOLEAN DEFAULT TRUE;"))
            else:
                print("ℹ️ Column show_on_marketplace already exists.")
                
            db.session.commit()
            print("✅ Events visibility migration completed successfully!")
            
        except Exception as e:
            db.session.rollback()
            print(f"❌ Migration error: {e}")
            sys.exit(1)

if __name__ == '__main__':
    run_migration()
