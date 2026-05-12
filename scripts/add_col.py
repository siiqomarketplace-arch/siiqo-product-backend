from app import create_app
from app.extensions import db
from sqlalchemy import text

app = create_app('development')

with app.app_context():
    try:
        db.session.execute(text("ALTER TABLE storefronts ADD COLUMN is_published BOOLEAN DEFAULT FALSE;"))
        db.session.commit()
        print("Column is_published added successfully.")
    except Exception as e:
        print(f"Error (might already exist): {e}")
        db.session.rollback()
