import sys
import os

sys.path.insert(0, os.path.abspath('.'))

from app import create_app
from app.models.user import User

app = create_app()

with app.app_context():
    users = User.query.all()
    print("=" * 40)
    print(f"Total Users in DB: {len(users)}")
    for u in users:
        print(f"- {u.email} (Role: {u.role})")
    print("=" * 40)
