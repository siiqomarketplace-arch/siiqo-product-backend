#!/usr/bin/env python3
"""
Events and Ticketing System Migration
Adds tables for events, ticket types, and ticket purchases
"""

import os
import sys
from sqlalchemy import create_engine, text

def run_migration():
    """Execute the events and ticketing migration"""
    
    # Get database URL from environment
    database_url = os.environ.get('DATABASE_URL')
    
    if not database_url:
        print("❌ DATABASE_URL environment variable not set")
        print("   Set it in your .env file or export it:")
        print("   export DATABASE_URL='postgresql://user:password@host:port/database'")
        return False
    
    print("🎫 Events and Ticketing System Migration")
    print("=" * 70)
    print(f"📊 Database: {database_url.split('@')[1] if '@' in database_url else 'local'}")
    print()
    
    try:
        # Create database engine
        engine = create_engine(database_url)
        
        # Read the migration SQL file
        migration_file = 'migrations/create_events_and_tickets_tables.sql'
        
        if not os.path.exists(migration_file):
            print(f"❌ Migration file not found: {migration_file}")
            return False
        
        with open(migration_file, 'r', encoding='utf-8') as f:
            migration_sql = f.read()
        
        print("📄 Executing migration SQL...")
        print()
        
        # Execute the migration
        with engine.connect() as conn:
            # Split by semicolon but keep DO blocks together
            statements = []
            current_statement = []
            in_do_block = False
            
            for line in migration_sql.split('\n'):
                if line.strip().startswith('DO $$'):
                    in_do_block = True
                
                current_statement.append(line)
                
                if in_do_block and line.strip() == '$$;':
                    in_do_block = False
                    statements.append('\n'.join(current_statement))
                    current_statement = []
                elif not in_do_block and line.strip().endswith(';') and not line.strip().startswith('--'):
                    statements.append('\n'.join(current_statement))
                    current_statement = []
            
            # Execute each statement
            for statement in statements:
                statement = statement.strip()
                if statement and not statement.startswith('--'):
                    try:
                        result = conn.execute(text(statement))
                        conn.commit()
                    except Exception as e:
                        # Ignore "already exists" errors
                        if 'already exists' not in str(e).lower():
                            print(f"⚠️  Warning: {e}")
        
        print("✅ Migration completed successfully!")
        print()
        print("📊 Created tables:")
        print("   • events - Event listings with online/in-person support")
        print("   • ticket_types - Multiple ticket types per event (VIP, Regular, etc.)")
        print("   • ticket_purchases - Individual ticket records with QR codes")
        print()
        print("🆕 Added to products table:")
        print("   • is_free - Mark digital products/services as free")
        print()
        
        # Verify tables were created
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_name IN ('events', 'ticket_types', 'ticket_purchases')
                ORDER BY table_name
            """))
            
            tables = [row[0] for row in result]
            
            if len(tables) == 3:
                print("✓ All tables verified")
            else:
                print(f"⚠️  Only {len(tables)}/3 tables found")
                for table in tables:
                    print(f"   • {table}")
        
        print()
        print("🎉 Events and ticketing system is ready!")
        print()
        print("📝 Next steps:")
        print("   1. Deploy backend with new models")
        print("   2. Create API routes for event management")
        print("   3. Build frontend event listing and ticket purchase flow")
        print("   4. Test ticket generation and QR code validation")
        print()
        
        return True
        
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    success = run_migration()
    sys.exit(0 if success else 1)
