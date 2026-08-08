import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.extensions import db
from sqlalchemy import text

app = create_app()

with app.app_context():
    try:
        with open('create_audit_table.sql', 'r') as f:
            sql = f.read()
        db.session.execute(text(sql))
        db.session.commit()
        print('\n✅ SUCCESS: Admin audit log table created!')
    except Exception as e:
        if 'already exists' in str(e).lower():
            print('\n✅ Table already exists (this is fine)')
        else:
            print(f'\n⚠️  Error: {e}')
        db.session.rollback()
