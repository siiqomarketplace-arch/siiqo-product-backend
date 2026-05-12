import os
import re
import sys
import uuid
from datetime import datetime

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import create_app
from app.extensions import db
from app.models.user import User, Storefront, UserRole
from app.models.product import Category, Product
from app.models.community import Article

def parse_values(values_str):
    records = []
    current_tuple = []
    current_value = ""
    in_string = False
    escape_next = False
    
    for char in values_str:
        if escape_next:
            current_value += char
            escape_next = False
            continue
            
        if char == '\\':
            current_value += char
            escape_next = True
            continue
            
        if char == "'":
            in_string = not in_string
            current_value += char
            continue
            
        if not in_string:
            if char == '(':
                current_tuple = []
                current_value = ""
            elif char == ')':
                current_tuple.append(current_value.strip())
                records.append(current_tuple)
                current_value = ""
            elif char == ',':
                if current_value != "":
                    current_tuple.append(current_value.strip())
                current_value = ""
            else:
                current_value += char
        else:
            current_value += char
    return records

def clean_val(val):
    val = val.strip()
    if val.startswith("'") and val.endswith("'"):
        return val[1:-1]
    if val.upper() == 'NULL':
        return None
    return val

def run_migration(sql_file_path):
    print(f"Starting migration from: {sql_file_path}")
    app = create_app()
    
    with app.app_context():
        users_added = 0
        articles_added = 0
        storefronts_added = 0
        
        with open(sql_file_path, 'r', encoding='utf-8', errors='ignore') as f:
            data = f.read()
            
        # Find all INSERT blocks: "INSERT INTO `table` (`cols`) VALUES (...);"
        insert_blocks = re.finditer(r'INSERT INTO `(\w+)` \((.*?)\) VALUES\s*(.*?);', data, re.IGNORECASE | re.DOTALL)
        
        for match in insert_blocks:
            table_name = match.group(1)
            cols_str = match.group(2)
            values_str = match.group(3)
            
            records = parse_values(values_str)
            
            if table_name == 'users':
                for rec in records:
                    try:
                        email = clean_val(rec[3])
                        existing = User.query.filter_by(email=email).first()
                        if existing: continue
                            
                        role_str = clean_val(rec[6]).lower()
                        role = UserRole.BUYER
                        if role_str == 'vendor': role = UserRole.VENDOR
                        elif role_str == 'admin': role = UserRole.ADMIN
                        elif role_str == 'superadmin': role = UserRole.SUPERADMIN
                            
                        user = User(
                            id=int(clean_val(rec[0])),
                            email=email,
                            phone=clean_val(rec[4]),
                            password_hash=clean_val(rec[5]),
                            role=role,
                            is_verified=(clean_val(rec[8]) == 'active')
                        )
                        db.session.add(user)
                        users_added += 1
                    except Exception as e:
                        print(f"Error migrating user {rec[3]}: {e}")
                        
            elif table_name == 'articles':
                for rec in records:
                    try:
                        article = Article(
                            id=int(clean_val(rec[0])),
                            title=clean_val(rec[1]),
                            slug=clean_val(rec[2]),
                            content=clean_val(rec[3]),
                            cover_image=clean_val(rec[5]),
                            is_published=(clean_val(rec[7]) == 'published')
                        )
                        author_id = clean_val(rec[6])
                        if author_id and author_id != 'None':
                            article.author_id = int(author_id)
                            
                        db.session.add(article)
                        articles_added += 1
                    except Exception as e:
                        print(f"Error migrating article {rec[1]}: {e}")

            elif table_name == 'storefronts':
                for rec in records:
                    try:
                        sf = Storefront(
                            id=int(clean_val(rec[0])),
                            vendor_id=int(clean_val(rec[1])),
                            store_name=clean_val(rec[2]),
                            store_slug=clean_val(rec[5]),
                            store_description=clean_val(rec[3]),
                            address=clean_val(rec[4]),
                            store_logo=clean_val(rec[6]),
                            banner_url=clean_val(rec[7]),
                            is_verified=(clean_val(rec[11]) == 'published')
                        )
                        db.session.add(sf)
                        storefronts_added += 1
                    except Exception as e:
                        print(f"Error migrating storefront {rec[2]}: {e}")

        try:
            db.session.commit()
            print("Migration completed successfully!")
            print(f"Migrated Users: {users_added}")
            print(f"Migrated Articles: {articles_added}")
            print(f"Migrated Storefronts: {storefronts_added}")
        except Exception as e:
            db.session.rollback()
            print(f"Migration failed during commit: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python migrate_from_sql.py <path_to_sql_dump>")
        sys.exit(1)
        
    sql_path = sys.argv[1]
    if not os.path.exists(sql_path):
        print(f"File not found: {sql_path}")
        sys.exit(1)
        
    run_migration(sql_path)
