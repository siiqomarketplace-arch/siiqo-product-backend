from app import create_app
from app.extensions import db
from app.models.user import User

app = create_app('development')
with app.app_context():
    keywords = ["okereke", "inno", "stillwalker", "ngozi", "tessy", "new"]
    
    users_to_delete = []
    for k in keywords:
        for u in User.query.filter(User.email.ilike(f'%{k}%')).all():
            if u not in users_to_delete:
                users_to_delete.append(u)
        for u in User.query.filter(User.first_name.ilike(f'%{k}%')).all():
            if u not in users_to_delete:
                users_to_delete.append(u)
        for u in User.query.filter(User.last_name.ilike(f'%{k}%')).all():
            if u not in users_to_delete:
                users_to_delete.append(u)

    if not users_to_delete:
        print("No matching users found.")
    else:
        print(f"Found {len(users_to_delete)} user(s) to delete:")
        for u in users_to_delete:
            print(f"  - {u.email} ({u.first_name} {u.last_name})")
        
        for u in users_to_delete:
            db.session.delete(u)
        db.session.commit()
        print("All matching users deleted successfully.")
