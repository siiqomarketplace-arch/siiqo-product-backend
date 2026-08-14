"""
Add subcategory column to articles table for grants filtering
Run this once: python add_subcategory_column.py
"""
import os
import sys
from sqlalchemy import create_engine, text

# Load DATABASE_URL from .env
from dotenv import load_dotenv
load_dotenv()

DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    print("❌ DATABASE_URL not found in .env")
    sys.exit(1)

print(f"🔗 Connecting to database...")
engine = create_engine(DATABASE_URL)

try:
    with engine.connect() as conn:
        # Check if column exists
        result = conn.execute(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name='articles' AND column_name='subcategory'
        """))
        
        if result.fetchone():
            print("✅ Column 'subcategory' already exists in articles table")
        else:
            # Add the column
            print("➕ Adding 'subcategory' column to articles table...")
            conn.execute(text("""
                ALTER TABLE articles 
                ADD COLUMN subcategory VARCHAR(100)
            """))
            conn.commit()
            print("✅ Column 'subcategory' added successfully!")
            
        print("\n📊 Checking articles table structure...")
        result = conn.execute(text("""
            SELECT column_name, data_type, is_nullable 
            FROM information_schema.columns 
            WHERE table_name='articles'
            ORDER BY ordinal_position
        """))
        
        print("\nArticles table columns:")
        for row in result:
            print(f"  - {row[0]}: {row[1]} (nullable: {row[2]})")
            
except Exception as e:
    print(f"❌ Error: {e}")
    sys.exit(1)

print("\n✅ Migration completed successfully!")
