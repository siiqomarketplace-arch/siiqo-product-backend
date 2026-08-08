"""
One-time migration runner - Run this via EB SSH or as a platform hook
Creates grants table with sample data
"""

import os
from app import create_app
from app.extensions import db
from sqlalchemy import text

def run_grants_migration():
    """Execute grants table migration"""
    app = create_app()
    
    with app.app_context():
        print("\n" + "="*60)
        print("GRANTS TABLE MIGRATION")
        print("="*60)
        
        try:
            # Read SQL migration file
            sql_file = 'migrations/create_grants_table.sql'
            print(f"Reading: {sql_file}")
            
            with open(sql_file, 'r', encoding='utf-8') as f:
                sql_content = f.read()
            
            # Execute migration
            print("Executing migration...")
            db.session.execute(text(sql_content))
            db.session.commit()
            print("✓ Migration completed successfully!")
            
            # Verify
            result = db.session.execute(text("""
                SELECT COUNT(*) as total,
                       COUNT(*) FILTER (WHERE status = 'open') as open,
                       COUNT(*) FILTER (WHERE featured = TRUE) as featured
                FROM grants
            """))
            
            row = result.fetchone()
            print(f"\nGrants created:")
            print(f"  - Total: {row[0]}")
            print(f"  - Open: {row[1]}")
            print(f"  - Featured: {row[2]}")
            
            print("\n" + "="*60)
            print("SUCCESS - Grants table is ready!")
            print("="*60 + "\n")
            
        except Exception as e:
            print(f"\n❌ Migration failed: {str(e)}")
            db.session.rollback()
            raise

if __name__ == '__main__':
    run_grants_migration()
