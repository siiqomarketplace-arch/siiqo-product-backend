import sqlite3
from werkzeug.security import generate_password_hash
conn = sqlite3.connect('instance/siiqo.db')
cursor = conn.cursor()

password_hash = generate_password_hash('Password123!')

# Update testvendor
cursor.execute("UPDATE users SET password_hash=? WHERE email='testvendor@siiqo.com'", (password_hash,))

# Create admin user if not exists
cursor.execute("SELECT * FROM admin_users WHERE email='admin@siiqo.com'")
if not cursor.fetchall():
    cursor.execute("INSERT INTO admin_users (name, email, password_hash, role) VALUES (?, ?, ?, ?)", ('Super Admin', 'admin@siiqo.com', password_hash, 'SUPERADMIN'))

conn.commit()
print("Passwords updated!")
