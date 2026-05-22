import sys
import os

sys.path.insert(0, os.path.abspath('.'))

from app import create_app
from app.extensions import db
from sqlalchemy import text

app = create_app()

with app.app_context():
    try:
        db.session.execute(text('ALTER TABLE users ADD COLUMN is_subscribed_to_broadcasts BOOLEAN DEFAULT TRUE;'))
        db.session.commit()
        print("Column added successfully.")
    except Exception as e:
        db.session.rollback()
        print(f"Error (may already exist): {e}")
