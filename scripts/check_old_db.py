import sqlite3
import os

db_path = os.path.join(os.path.dirname(__file__), 'instance', 'siiqo.db')

print(f"\nChecking old SQLite database: {db_path}")
print("=" * 60)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# List all tables
cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = cursor.fetchall()
print(f"\nTables found: {len(tables)}")
for t in tables:
    cursor.execute(f"SELECT COUNT(*) FROM '{t[0]}'")
    count = cursor.fetchone()[0]
    print(f"  - {t[0]}: {count} rows")

# Show users specifically
print("\n===== EXISTING USERS =====")
try:
    cursor.execute("SELECT id, email, first_name, last_name, role, is_verified, created_at FROM users")
    users = cursor.fetchall()
    if users:
        for u in users:
            print(f"  ID: {u[0]} | Email: {u[1]} | Name: {u[2]} {u[3]} | Role: {u[4]} | Verified: {u[5]} | Created: {u[6]}")
    else:
        print("  No users found in the old database.")
except Exception as e:
    print(f"  Error reading users: {e}")

conn.close()
print("\n" + "=" * 60)
