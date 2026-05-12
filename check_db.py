from app import create_app
from app.extensions import db
from sqlalchemy import inspect, text

app = create_app('development')
with app.app_context():
    inspector = inspect(db.engine)
    tables = inspector.get_table_names()
    print("\n===== TABLES IN YOUR AWS DATABASE =====")
    if tables:
        for t in tables:
            count = db.session.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar()
            print(f"  - {t}  ({count} rows)")
    else:
        print("  No tables found!")
    print("=======================================\n")
