"""
Grant Database Migration Script
Creates the grants table and inserts sample data
Run this script to set up the grants database
"""

import os
import sys
from sqlalchemy import create_engine, text
from datetime import datetime

# Database connection details
DB_USER = os.getenv('RDS_USER', 'postgres')
DB_PASSWORD = os.getenv('RDS_PASSWORD', 'BW5t2sWw0NT1KMGrCHfD%BP6')
DB_HOST = os.getenv('RDS_HOST', 'database-1.c8zsq20ocq7p.us-east-1.rds.amazonaws.com')
DB_PORT = os.getenv('RDS_PORT', '5432')
DB_NAME = os.getenv('RDS_DB', 'postgres')

DATABASE_URL = f'postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}'


def run_migration():
    """Execute the grants table migration."""
    print("=" * 60)
    print("GRANT DATABASE MIGRATION")
    print("=" * 60)
    print(f"Database: {DB_HOST}/{DB_NAME}")
    print(f"Timestamp: {datetime.now().isoformat()}")
    print("=" * 60)
    
    try:
        # Create engine
        engine = create_engine(DATABASE_URL)
        print("✓ Database connection established")
        
        with engine.connect() as conn:
            # Read the SQL migration file
            sql_file = 'migrations/create_grants_table.sql'
            print(f"\nReading migration file: {sql_file}")
            
            with open(sql_file, 'r', encoding='utf-8') as f:
                sql_content = f.read()
            
            # Execute the migration
            print("\nExecuting migration...")
            conn.execute(text(sql_content))
            conn.commit()
            print("✓ Migration executed successfully")
            
            # Verify the table was created
            print("\nVerifying grants table...")
            result = conn.execute(text("""
                SELECT 
                    COUNT(*) as total_grants,
                    COUNT(*) FILTER (WHERE status = 'open') as open_grants,
                    COUNT(*) FILTER (WHERE featured = TRUE) as featured_grants
                FROM grants
            """))
            
            row = result.fetchone()
            print("✓ Grants table verified")
            print(f"  - Total grants: {row[0]}")
            print(f"  - Open grants: {row[1]}")
            print(f"  - Featured grants: {row[2]}")
            
            # Show sample data
            print("\nSample grants:")
            result = conn.execute(text("""
                SELECT id, name, amount, status, country
                FROM grants
                ORDER BY created_at DESC
                LIMIT 5
            """))
            
            for row in result:
                print(f"  [{row[0]}] {row[1]}")
                print(f"      Amount: {row[2]} | Status: {row[3]} | Country: {row[4]}")
        
        print("\n" + "=" * 60)
        print("MIGRATION COMPLETED SUCCESSFULLY")
        print("=" * 60)
        print("\nNext steps:")
        print("1. Update app/models/__init__.py to import Grant model")
        print("2. Create API routes in app/routes/grants.py")
        print("3. Test the grants endpoints")
        print("=" * 60)
        
        return True
        
    except Exception as e:
        print("\n" + "=" * 60)
        print("MIGRATION FAILED")
        print("=" * 60)
        print(f"Error: {str(e)}")
        print("\nTroubleshooting:")
        print("1. Check database connection details")
        print("2. Ensure admin_users table exists (foreign key dependency)")
        print("3. Verify PostgreSQL version supports arrays")
        print("=" * 60)
        return False


def rollback_migration():
    """Rollback the grants table migration."""
    print("=" * 60)
    print("GRANT DATABASE ROLLBACK")
    print("=" * 60)
    
    confirm = input("Are you sure you want to DROP the grants table? (yes/no): ")
    if confirm.lower() != 'yes':
        print("Rollback cancelled.")
        return
    
    try:
        engine = create_engine(DATABASE_URL)
        print("✓ Database connection established")
        
        with engine.connect() as conn:
            print("\nDropping grants table...")
            conn.execute(text("DROP TABLE IF EXISTS grants CASCADE"))
            conn.commit()
            print("✓ Grants table dropped successfully")
        
        print("\n" + "=" * 60)
        print("ROLLBACK COMPLETED")
        print("=" * 60)
        
    except Exception as e:
        print(f"\nRollback failed: {str(e)}")


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'rollback':
        rollback_migration()
    else:
        success = run_migration()
        sys.exit(0 if success else 1)
