#!/usr/bin/env python3
"""
Events and Ticketing Enhancements Migration
Adds organizer details, custom registration fields, multi-location schedules, and guest order support.
"""

import os
import sys

# Try loading .env automatically
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

try:
    from sqlalchemy import create_engine, text
except ImportError:
    print("❌ Error: 'sqlalchemy' module is not installed.")
    sys.exit(1)

def run_migration():
    database_url = os.environ.get('DATABASE_URL')
    
    if not database_url:
        print("❌ DATABASE_URL environment variable not set")
        return False
    
    print("🎟️ Events & Ticketing Enhancements Migration")
    print("=" * 70)
    print(f"📊 Database: {database_url.split('@')[1] if '@' in database_url else 'local'}")
    print()
    
    try:
        engine = create_engine(database_url)
        migration_file = 'migrations/enhance_events_and_tickets.sql'
        
        if not os.path.exists(migration_file):
            print(f"❌ Migration file not found: {migration_file}")
            return False
        
        with open(migration_file, 'r', encoding='utf-8') as f:
            migration_sql = f.read()
        
        with engine.connect() as conn:
            for statement in migration_sql.split(';'):
                statement = statement.strip()
                if statement and not statement.startswith('--'):
                    try:
                        conn.execute(text(statement))
                        conn.commit()
                        print(f"✓ Executed: {statement[:60]}...")
                    except Exception as e:
                        if 'already exists' not in str(e).lower():
                            print(f"⚠️  Note: {e}")
        
        print("\n✅ Events & Ticketing Enhancements Migration Complete!")
        return True
        
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == '__main__':
    success = run_migration()
    sys.exit(0 if success else 1)
